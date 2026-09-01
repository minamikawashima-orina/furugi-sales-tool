"""
Gemini APIとの通信をまとめたモジュール。

役割:
    メルカリのスクリーンショット画像（バイト列）を受け取り、Gemini APIに
    解析させて、商品情報（ブランド・ジャンル・価格など）を日本語キーの
    dict形式で返す。

    売却後画面の解析（analyze_mercari_image）に加えて、出品画面の解析
    （analyze_mercari_listing_image）も提供する。

app.py（画面側）は、このモジュールの analyze_mercari_image() /
analyze_mercari_listing_image() を呼び出すだけで解析結果を受け取れるように
している。

使用SDK: google-genai（旧 google-generativeai は2025年11月末で
サポート終了・アーカイブ済みのため使用しない）
"""

import os
import base64

import httpx
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


# Gemini APIへのリクエストが応答を返さないまま無期限に待機し続けることが
# ないよう、タイムアウト（秒）を明示的に設定する。
#
# _smoketest.py（テキストのみ）ではtimeout=20、_smoketest_image.py（画像あり）
# ではtimeout=30を使用しているが、実際の解析ではさらに構造化出力（JSON
# スキーマ指定）も伴い、画像の内容によってはより時間がかかる場合があるため、
# 余裕を持たせてこの値を設定している。
#
# 売却情報解析（analyze_mercari_image）・出品情報解析
# （analyze_mercari_listing_image）の両方でこの値を共通利用する。
GEMINI_REQUEST_TIMEOUT_SECONDS = 45

# タイムアウト発生時にStreamlit画面へ表示する、ユーザー向けの分かりやすい
# メッセージ。原因（HTTPクライアントの例外の詳細等）はログにも画面にも
# 出さず、この文言だけを表示する。
TIMEOUT_ERROR_MESSAGE = (
    "Geminiの解析に時間がかかりすぎたため、処理を終了しました。もう一度お試しください。"
)


def _is_timeout_error(error: BaseException) -> bool:
    """
    Gemini APIへのリクエストがタイムアウトしたことを表す例外かどうかを判定する。

    使用しているgoogle-genai SDKは、内部で使用しているHTTPクライアント
    （httpx）のタイムアウト例外を、SDK独自のエラークラスにラップすることが
    あり、かつそのクラスはSDKの非公開モジュール（アンダースコア始まりの
    パッケージ）にしか存在しない。そのため特定のクラスを直接インポートして
    isinstanceで判定するのではなく、次のいずれかで判定する（SDKの内部実装が
    変わってもタイムアウト検知が壊れにくいようにするため）。

    - httpx（google-genaiが内部で使用するHTTPクライアント）自体の
      タイムアウト例外（httpx.TimeoutException）であるか
    - 例外クラス名に "Timeout" が含まれるか（SDKがラップした場合や、
      将来的に別のクラスでラップされた場合の保険）
    """
    if isinstance(error, httpx.TimeoutException):
        return True
    return "timeout" in type(error).__name__.lower()


def _create_interaction(prompt: str, image_b64: str, mime_type: str, schema: dict):
    """
    Gemini APIへの画像解析リクエスト（interactions.create呼び出し）を、
    共通のタイムアウト設定・タイムアウト時のエラーメッセージ付きで実行する。

    analyze_mercari_image() / analyze_mercari_listing_image() の両方から
    共通で呼び出すことで、タイムアウト設定を一箇所（GEMINI_REQUEST_TIMEOUT_SECONDS）
    にまとめて管理できるようにしている。

    Args:
        prompt: Geminiに渡すプロンプト文字列
        image_b64: Base64エンコードされた画像データ
        mime_type: 画像のMIMEタイプ
        schema: 応答に期待するJSONスキーマ（pydanticモデルのmodel_json_schema()）

    Raises:
        RuntimeError: APIキーが未設定の場合
        TimeoutError: GEMINI_REQUEST_TIMEOUT_SECONDS秒以内にGemini APIから
            応答が返ってこなかった場合（画面にはTIMEOUT_ERROR_MESSAGEを表示する）
    """
    client = _get_client()
    try:
        return client.interactions.create(
            model=MODEL_NAME,
            input=[
                {"type": "text", "text": prompt},
                {"type": "image", "data": image_b64, "mime_type": mime_type},
            ],
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": schema,
            },
            timeout=GEMINI_REQUEST_TIMEOUT_SECONDS,
        )
    except Exception as e:
        if _is_timeout_error(e):
            raise TimeoutError(TIMEOUT_ERROR_MESSAGE) from e
        raise


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
        TimeoutError: Gemini APIの応答がGEMINI_REQUEST_TIMEOUT_SECONDS秒以内に
            返ってこなかった場合
        ValueError: Geminiからの応答をJSONとして解釈できなかった場合
    """
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    interaction = _create_interaction(
        PROMPT, image_b64, mime_type, MercariItem.model_json_schema()
    )

    try:
        item = MercariItem.model_validate_json(interaction.output_text)
    except Exception as e:
        raise ValueError(
            f"Geminiの応答をJSONとして解釈できませんでした: {e}\n"
            f"応答内容: {getattr(interaction, 'output_text', interaction)}"
        ) from e

    return {FIELD_LABELS[key]: value for key, value in item.model_dump().items()}


class MercariListingItem(BaseModel):
    """Geminiに読み取らせる3項目（メルカリの出品画面に表示される項目）。

    出品日時はGeminiに読み取らせず、登録実行時にPython側で現在時刻
    （日本時間）を取得するため、取得項目に含めない。
    """

    title: str = Field(description="商品タイトル")
    price: str = Field(description="出品価格")
    brand: str = Field(description="ブランド（画像から読み取れない場合は「不明」）")


# 画面表示用: 英語フィールド名 → 日本語ラベル（出品画面解析用）
LISTING_FIELD_LABELS = {
    "title": "商品タイトル",
    "price": "出品価格",
    "brand": "ブランド",
}

LISTING_PROMPT = """
これはフリマアプリ「メルカリ」の出品画面（出品時の商品情報を入力・確認する画面）の
スクリーンショットです。
画像から次の3項目を読み取ってください。

- 商品タイトル: 商品名
- 出品価格: 出品時に設定されている価格
- ブランド: 商品のブランド名（画像にブランド名が表示されている場合のみ）

重要な注意事項:
- 画像に実際に表示されている情報だけを読み取ってください。
- ブランドが画像に表示されていない、または「ブランドなし」等と表示されている場合は、
  推測せずに "不明" としてください。
- 商品タイトルや価格についても、画像に表示されていない項目は絶対に推測や計算で
  値を埋めないでください。その項目は "不明" としてください。
- 数値の項目も文字列として出力してください（単位があれば単位も含める）。
"""


def analyze_mercari_listing_image(image_bytes: bytes, mime_type: str) -> dict:
    """
    メルカリの出品画面のスクリーンショット画像を解析し、結果を日本語キーのdictで返す。

    Args:
        image_bytes: 画像ファイルのバイト列
        mime_type: 画像のMIMEタイプ（例: "image/png", "image/jpeg"）

    Returns:
        dict: {"商品タイトル": ..., "出品価格": ..., "ブランド": ...} の3項目
            （ブランドが画像から読み取れなかった場合は "不明" が入る）

    Raises:
        RuntimeError: APIキーが未設定の場合
        TimeoutError: Gemini APIの応答がGEMINI_REQUEST_TIMEOUT_SECONDS秒以内に
            返ってこなかった場合
        ValueError: Geminiからの応答をJSONとして解釈できなかった場合
    """
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    interaction = _create_interaction(
        LISTING_PROMPT, image_b64, mime_type, MercariListingItem.model_json_schema()
    )

    try:
        item = MercariListingItem.model_validate_json(interaction.output_text)
    except Exception as e:
        raise ValueError(
            f"Geminiの応答をJSONとして解釈できませんでした: {e}\n"
            f"応答内容: {getattr(interaction, 'output_text', interaction)}"
        ) from e

    return {LISTING_FIELD_LABELS[key]: value for key, value in item.model_dump().items()}
