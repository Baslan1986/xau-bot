"""Name forms -> one person, with no guessing.

A person can be written several ways (a first name, a title + name, a family name). Each known
form is stored as an alias. Matching is EXACT on a normalised form; normalisation only removes
spelling noise (diacritics, tatweel, alef/hamza variants, extra spaces), never words. A form that
matches nobody, or more than one person, becomes an open question for an admin, never a guess.
"""
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import NameQuestion, PersonAlias, User

_DIACRITICS = re.compile(r"[ً-ْٰـ]")  # harakat, superscript alef, tatweel
_ALEF = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"})


def normalise(form: str) -> str:
    s = _DIACRITICS.sub("", form or "").translate(_ALEF)
    s = s.replace(".", " ")
    return re.sub(r"\s+", " ", s).strip().casefold()


class AliasConflict(ValueError):
    pass


def set_aliases(db: Session, user: User, forms: list[str]) -> None:
    """Replace a person's aliases. A form already owned by someone else is a conflict."""
    for a in db.scalars(select(PersonAlias).where(PersonAlias.user_id == user.id)):
        db.delete(a)
    db.flush()
    for form in {f.strip() for f in forms + [user.name] if f and f.strip()}:
        norm = normalise(form)
        other = db.scalars(select(PersonAlias).where(PersonAlias.tenant_id == user.tenant_id,
                                                     PersonAlias.norm == norm)).first()
        if other and other.user_id != user.id:
            raise AliasConflict(f"الاسم '{form}' مسجل لشخص ثاني")
        if not other:
            db.add(PersonAlias(tenant_id=user.tenant_id, user_id=user.id, form=form, norm=norm))
            db.flush()


def lookup(db: Session, tenant_id: str, form: str) -> User | None:
    """Exact match on a known form; None if unknown. Never creates a question."""
    hit = db.scalars(select(PersonAlias).where(PersonAlias.tenant_id == tenant_id,
                                               PersonAlias.norm == normalise(form))).first()
    return db.get(User, hit.user_id) if hit else None


def resolve(db: Session, tenant_id: str, form: str, context: str | None = None) -> dict:
    norm = normalise(form)
    if not norm:
        return {"status": "invalid"}
    hit = db.scalars(select(PersonAlias).where(PersonAlias.tenant_id == tenant_id,
                                               PersonAlias.norm == norm)).first()
    if hit:
        u = db.get(User, hit.user_id)
        return {"status": "matched", "attendUserId": u.id, "personRef": u.person_ref}
    q = db.scalars(select(NameQuestion).where(NameQuestion.tenant_id == tenant_id,
                                              NameQuestion.norm == norm,
                                              NameQuestion.status == "open")).first()
    if not q:
        q = NameQuestion(tenant_id=tenant_id, form=form.strip()[:120], norm=norm,
                         context=(context or "")[:200] or None)
        db.add(q)
        db.flush()
    return {"status": "unknown", "questionId": q.id}


def answer(db: Session, q: NameQuestion, user: User) -> None:
    """Admin confirms which person a form means; it becomes an alias so it is never asked again."""
    if user.tenant_id != q.tenant_id:
        raise AliasConflict("الشخص من جهة ثانية")
    try:
        db.add(PersonAlias(tenant_id=q.tenant_id, user_id=user.id, form=q.form, norm=q.norm))
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise AliasConflict("هذا الاسم صار مربوط بشخص") from e
    q.status, q.user_id = "resolved", user.id
