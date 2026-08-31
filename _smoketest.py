import os, time
from dotenv import load_dotenv
load_dotenv()

key = os.getenv("GEMINI_API_KEY")
model = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")
print("APIキー読み込み:", ("OK (長さ=%d)" % len(key)) if key else "NG (未設定)", flush=True)
print("モデル名:", model, flush=True)

from google import genai
client = genai.Client(api_key=key)

start = time.time()
try:
    interaction = client.interactions.create(
        model=model,
        input="Say 'ok' only.",
        timeout=20,
    )
    print("成功: %.1fs" % (time.time() - start), flush=True)
    print(interaction.output_text, flush=True)
except Exception as e:
    print("失敗: %.1fs" % (time.time() - start), flush=True)
    print(type(e).__name__, "-", e, flush=True)
