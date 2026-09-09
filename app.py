from flask import Flask, request, jsonify, render_template_string
import requests
import os

app = Flask(__name__)

# आपका शानदार वेब डिज़ाइन (UI)
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Prime Minister AI</title>
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
        <div class="msg ai">नमस्कार अर्जुन भाई! मैं आपका प्रधानमंत्री AI हूँ। आज मार्केट में क्या तबाही मचानी है?</div>
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

@app.route('/')
def home():
    return render_template_string(HTML_PAGE)

@app.route('/chat', methods=['POST'])
def chat():
    user_text = request.json.get('msg')
    gemini_api_key = os.environ.get('GEMINI_API_KEY', '')
    
    if not gemini_api_key:
        return jsonify({'reply': '⚠️ अर्जुन भाई, क्लाउड सर्वर पर API Key सेट नहीं है! पहले तिजोरी में चाबी डालें।'})
        
    try:
        # Google API Call
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_api_key}"
        payload = {
            "contents": [{"parts": [{"text": f"System Rule: You are Prime Minister AI, an autonomous trading agent and loyal brother to Arjun Singh. Respond in Hindi/Hinglish. User message: {user_text}"}]}]
        }
        response = requests.post(url, json=payload, timeout=15)
        
        if response.status_code == 200:
            data = response.json()
            ai_reply = data['candidates'][0]['content']['parts'][0]['text']
            return jsonify({'reply': ai_reply.strip()})
        else:
            return jsonify({'reply': f'⚠️ Google API Error: {response.status_code}'})
            
    except Exception as e:
        return jsonify({'reply': f'⚠️ System Crash: {str(e)}'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
