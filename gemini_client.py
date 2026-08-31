"""
Gemini APIとの通信をまとめたモジュール。

役割:
    メルカリのスクリーンショット画像（バイト列）を受け取り、Gemini APIに
    解析させて、商品情報（ブランド・ジャンル・価格など）を日本語キーの
    dict形式で返す。

app.py（画面側）は、このモジュールの analyze_mercari_image() を
呼び出すだけで解析結果を受け取れるようにしている。

使用SDK: google-genai（旧 google-generativeai は2025年11月末で
サポート終了・アーカイブ済みのため使用しない）
"""

import os
import base64

from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field

# .env ファイルから環境変数を読み込む（GEMINI_API_KEYなど）
load_dotenv()


def _get_api_key() -> str | None:
    """GEMINI_API_KEYを次の優先順位で取得する。

    1. Streamlit Community CloudのSecrets（st.secrets["GEMINI_API_KEY"]）
    2. ローカル環境の.env / 環境変数（GEMINI_API_KEY）

    streamlitが未インストールの場合や、Streamlit実行環境外（他のスクリプトや
    テストからgemini_client.pyを直接使う場合）、またはSecrets未設定の場合は、
    エラーにせず.env側の値へフォールバックする。
    """
    try:
        import streamlit as st

        if "GEMINI_API_KEY" in st.secrets:
            return st.secrets["GEMINI_API_KEY"]
    except Exception:
        # streamlit未インストール／Streamlit実行環境外／secrets.toml未作成など。
        # いずれの場合もエラーにせず.env側の取得にフォールバックする。
        pass

    return os.getenv("GEMINI_API_KEY")


API_KEY = _get_api_key()
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")

_client = None  # genai.Client は初回利用時に作成する（遅延初期化）


def _get_client() -> genai.Client:
    """genai.Clientを作成（未作成の場合のみ）して返す。"""
    global _client
    if _client is None:
        if not API_KEY:
            raise RuntimeError(
                "GEMINI_API_KEYが設定されていません。.envファイルを作成し、"
                "APIキーを設定してください（.env.exampleを参考にしてください）。"
            )
        _client = genai.Client(api_key=API_KEY)
    return _client


class MercariItem(BaseModel):
    """Geminiに読み取らせる4項目（メルカリの売却後画面に実際に表示される項目）。

    販売手数料・利益はGoogleスプレッドシート側の数式で自動計算されるため、
    Geminiには読み取らせない（取得項目に含めない）。
    """

    title: str = Field(description="商品タイトル")
    sold_price: str = Field(description="売却価格")
    purchased_at: str = Field(description="購入日時")
    shipping_fee: str = Field(description="配送料")


# 画面表示用: 英語フィールド名 → 日本語ラベル
FIELD_LABELS = {
    "title": "商品タイトル",
    "sold_price": "売却価格",
    "purchased_at": "購入日時",
    "shipping_fee": "配送料",
}

PROMPT = """
これはフリマアプリ「メルカリ」の売却後画面（取引が完了した商品の画面）の
スクリーンショットです。
画像から次の4項目を読み取ってください。

- 商品タイトル: 商品名
- 売却価格: 実際に売れた価格
- 購入日時: 購入者が購入した日時
- 配送料: 配送・送料として表示されている金額

重要な注意事項:
- 画像に実際に表示されている情報だけを読み取ってください。
- 画像に表示されていない項目については、絶対に推測や計算で値を埋めないでください。
  その項目は "不明" としてください。
- 数値の項目も文字列として出力してください（単位があれば単位も含める）。
- 販売手数料・利益はスプレッドシート側で自動計算するため、読み取り不要です。
"""


def analyze_mercari_image(image_bytes: bytes, mime_type: str) -> dict:
    """
    メルカリのスクリーンショット画像を解析し、結果を日本語キーのdictで返す。

    Args:
        image_bytes: 画像ファイルのバイト列
        mime_type: 画像のMIMEタイプ（例: "image/png", "image/jpeg"）

    Returns:
        dict: {"商品タイトル": ..., "売却価格": ..., "購入日時": ..., "配送料": ...} の4項目

    Raises:
        RuntimeError: APIキーが未設定の場合
        ValueError: Geminiからの応答をJSONとして解釈できなかった場合
    """
    client = _get_client()
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    interaction = client.interactions.create(
        model=MODEL_NAME,
        input=[
            {"type": "text", "text": PROMPT},
            {"type": "image", "data": image_b64, "mime_type": mime_type},
        ],
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": MercariItem.model_json_schema(),
        },
    )

    try:
        item = MercariItem.model_validate_json(interaction.output_text)
    except Exception as e:
        raise ValueError(
            f"Geminiの応答をJSONとして解釈できませんでした: {e}\n"
            f"応答内容: {getattr(interaction, 'output_text', interaction)}"
        ) from e

    return {FIELD_LABELS[key]: value for key, value in item.model_dump().items()}
