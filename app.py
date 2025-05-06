from flask import Flask, request
import requests

app = Flask(__name__)

BOT_TOKEN = '7842054486:AAFVkS1e4fgkHMV-y8X_FLqsyKDRpkc4NMo'
CHAT_ID = '1959973966'

def send_telegram(message):
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    payload = {'chat_id': CHAT_ID, 'text': message}
    requests.post(url, data=payload)

@app.route('/')
def home():
    return 'Bot is live!'

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    message = data.get('message', '📡 TradingView alert received.')
    send_telegram(message)
    return 'ok'
