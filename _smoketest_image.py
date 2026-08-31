"""
画像1枚をGemini APIに送る最小構成のテスト。

使い方:
    # ダミー画像（1x1のPNG）で試す場合
    /opt/anaconda3/bin/python3 -u _smoketest_image.py

    # 手元にあるメルカリのスクショで試す場合
    /opt/anaconda3/bin/python3 -u _smoketest_image.py /path/to/screenshot.png
"""
import base64
import mimetypes
import os
import sys
import time

from dotenv import load_dotenv

# .envの場所をこのファイルからの絶対パスで明示的に指定する
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(PROJECT_DIR, ".env")
load_dotenv(dotenv_path=ENV_PATH)

# 1x1の透明PNG（ダミー画像。引数で画像パスを指定しなかった場合に使う）
_DUMMY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

if len(sys.argv) > 1:
    image_path = sys.argv[1]
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    mime_type = mimetypes.guess_type(image_path)[0] or "image/png"
    print(f"画像ファイル: {image_path} ({len(image_bytes)} bytes, {mime_type})", flush=True)
else:
    image_bytes = base64.b64decode(_DUMMY_PNG_B64)
    mime_type = "image/png"
    print(f"ダミー画像を使用します ({len(image_bytes)} bytes, {mime_type})", flush=True)

key = os.getenv("GEMINI_API_KEY")
model = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")
print(".envのパス:", ENV_PATH, flush=True)
print("APIキー読み込み:", ("OK (長さ=%d)" % len(key)) if key else "NG (未設定)", flush=True)
print("モデル名:", model, flush=True)

from google import genai

client = genai.Client(api_key=key)
image_b64 = base64.b64encode(image_bytes).decode("utf-8")

start = time.time()
try:
    interaction = client.interactions.create(
        model=model,
        input=[
            {"type": "text", "text": "この画像には何が写っていますか？一言で答えてください。"},
            {"type": "image", "data": image_b64, "mime_type": mime_type},
        ],
        timeout=30,  # 30秒でタイムアウトさせる
    )
    print("成功: %.1fs" % (time.time() - start), flush=True)
    print(interaction.output_text, flush=True)
except Exception as e:
    print("失敗: %.1fs" % (time.time() - start), flush=True)
    print(type(e).__name__, "-", e, flush=True)
