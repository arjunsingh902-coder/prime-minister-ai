from flask import Flask, request, jsonify, render_template_string
import requests
import os
import hmac
import hashlib
import time

app = Flask(__name__)

HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Prime Minister AI - Trading Room</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { background-color: #17181e; color: #fff; font-family: sans-serif; padding: 15px; margin: 0; }
        #chat { height: 75vh; overflow-y: auto; margin-bottom: 15px; padding: 10px; border-radius: 10px; background-color: #20232b; box-shadow: inset 0 0 10px rgba(0,0,0,0.5); }
        .msg { margin: 10px 0; padding: 12px; border-radius: 8px; max-width: 85%; line-height: 1.4; font-size: 15px; }
        .user { background-color: #2962ff; margin-left: auto; border-bottom-right-radius: 0; }
        .ai { background-color: #00b184; color: #fff; margin-right: auto; border-bottom-left-radius: 0; }
        .input-area { display: flex; gap: 10px; }
        input { flex: 1; padding: 15px; border-radius: 25px; border: none; background-color: #2d313d; color: white; outline: none; font-size: 15px; }
        button { padding: 15px 25px; background-color: #2962ff; color: white; border: none; border-radius: 25px; font-weight: bold; }
    </style>
</head>
<body>
    <h2 style="text-align: center; color: #00b184; margin-top: 5px;">👑 Prime Minister AI</h2>
    <div id="chat">
        <div class="msg ai">नमस्कार अर्जुन भाई! मैं तैयार हूँ। अगर डेल्टा ने मुझे रोका, तो मैं तुरंत आपको नया IP दे दूँगा।</div>
    </div>
    <div class="input-area">
        <input type="text" id="userInput" placeholder="अपना हुक्म दें...">
        <button onclick="sendMsg()">Send</button>
    </div>

    <script>
        function sendMsg() {
            let text = document.getElementById('userInput').value;
            if(!text) return;
            
            let chatBox = document.getElementById('chat');
            chatBox.innerHTML += '<div class="msg user">' + text + '</div>';
            document.getElementById('userInput').value = '';
            chatBox.scrollTop = chatBox.scrollHeight;
            
            fetch('/chat', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({msg: text})
            })
            .then(res => res.json())
            .then(data => {
                chatBox.innerHTML += '<div class="msg ai">' + data.reply + '</div>';
                chatBox.scrollTop = chatBox.scrollHeight;
            })
            .catch(err => {
                chatBox.innerHTML += '<div class="msg ai" style="background-color: #ff453a;">⚠️ नेटवर्क एरर! सर्वर से कनेक्ट नहीं हो पाया।</div>';
            });
        }
    </script>
</body>
</html>
"""

def get_live_market_data():
    cmc_key = os.environ.get('CMC_API_KEY', '').strip()
    if not cmc_key:
        return "\n[CMC Data: Unavailable]"
    try:
        url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest?symbol=BTC,ETH,SOL"
        headers = {"X-CMC_PRO_API_KEY": cmc_key, "Accept": "application/json"}
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json().get('data', {})
            market_info = "\n\n[FULL MARKET DATA (CMC):\n"
            for coin in ["BTC", "ETH", "SOL"]:
                if coin in data:
                    price = data[coin]['quote']['USD']['price']
                    change = data[coin]['quote']['USD']['percent_change_24h']
                    market_info += f"- {coin}: Price=${price:.2f}, 24h Change={change:.2f}%\n"
            return market_info + "]\n"
    except:
        pass
    return "\n[CMC Data Unavailable]"

def get_delta_balance():
    api_key = os.environ.get('DELTA_API_KEY', '').strip()
    api_secret = os.environ.get('DELTA_API_SECRET', '').strip()
    
    if not api_key or not api_secret:
        return "\n[Delta Balance: Keys Missing]"
        
    try:
        timestamp = str(int(time.time()))
        method = "GET"
        path = "/v2/wallet/balances"
        signature_data = method + timestamp + path + ""
        
        signature = hmac.new(
            api_secret.encode('utf-8'),
            signature_data.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        headers = {
            "api-key": api_key,
            "timestamp": timestamp,
            "signature": signature,
            "Accept": "application/json"
        }
        
        url = f"https://api.india.delta.exchange{path}"
        res = requests.get(url, headers=headers, timeout=10)
        
        if res.status_code == 200:
            data = res.json().get('result', [])
            deriv_bal = 0.0
            main_bal = 0.0
            for bal in data:
                symbol = bal.get('asset_symbol', '').upper()
                if symbol in ['USD', 'USDT']:
                    w_type = str(bal.get('wallet_type', '')).lower()
                    available = float(bal.get('available_balance', 0.0))
                    if 'deriv' in w_type or 'future' in w_type or 'margin' in w_type or w_type == '2':
                        deriv_bal += available
                    else:
                        main_bal += available
            return f"\n[SYSTEM HEALTH REPORT:\nDerivatives Wallet Balance: ${deriv_bal:.2f}\nMain/Spot Wallet Balance: ${main_bal:.2f}]\n"
        
        elif res.status_code == 401:
            # जादू यहाँ है: अगर 401 आया, तो अपना नया IP निकालो
            try:
                my_new_ip = requests.get('https://api.ipify.org', timeout=5).text
                return f"\n[URGENT 401 ERROR: Tell Arjun Bhai that Delta blocked the IP. Give him this EXACT NEW IP: {my_new_ip} and tell him to update it in Delta App.]\n"
            except:
                return "\n[Delta 401 Error. IP couldn't be fetched.]\n"
        else:
            return f"\n[Delta API Error: {res.status_code}]"
    except Exception as e:
        return f"\n[Delta Offline / Timeout: {str(e)}]"

@app.route('/')
def home():
    return render_template_string(HTML_PAGE)

@app.route('/chat', methods=['POST'])
def chat():
    user_text = request.json.get('msg')
    gemini_api_key = os.environ.get('GEMINI_API_KEY', '').strip()
    
    if not gemini_api_key:
        return jsonify({'reply': '⚠️ अर्जुन भाई, API Key सेट नहीं है!'})
        
    try:
        market_data = get_live_market_data()
        delta_data = get_delta_balance()
        
        full_prompt = f"User message: {user_text}{market_data}{delta_data}"

        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": gemini_api_key
        }
        
        payload = {
            "system_instruction": {
                "parts": [{"text": "You are Prime Minister AI, trading agent and loyal brother to Arjun Singh. You have access to LIVE MARKET DATA and SYSTEM HEALTH REPORT. If you see [URGENT 401 ERROR] in the report, immediately alert Arjun in Hindi. Tell him the exact new IP address provided in the bracket and ask him to update it in his Delta Exchange app's API settings so you can trade."}]
            },
            "contents": [
                {"parts": [{"text": full_prompt}]}
            ]
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            ai_reply = data['candidates'][0]['content']['parts'][0]['text']
            return jsonify({'reply': ai_reply.strip()})
        else:
            return jsonify({'reply': f'⚠️ Google API Error {response.status_code}: {response.text}'})
            
    except requests.exceptions.ReadTimeout:
        return jsonify({'reply': '⚠️ गूगल के सर्वर पर अभी बहुत ज्यादा ट्रैफिक है। कृपया 1 मिनट रुक कर दोबारा पूछें।'})
    except Exception as e:
        return jsonify({'reply': f'⚠️ System Crash: {str(e)}'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
