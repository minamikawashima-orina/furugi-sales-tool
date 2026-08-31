"""
Googleスプレッドシートとの連携をまとめたモジュール。

役割:
    サービスアカウント認証でGoogle Sheetsに接続し、B列「商品名」を
    Geminiの「商品タイトル」で検索して対象行を特定し、P列「売却日」・
    Q列「売却価格」・S列「配送料」だけを書き込む。

    販売手数料・利益はスプレッドシート側の数式で自動計算される想定の
    ため、このモジュールからは一切書き込まない。

app.py（画面側）は、このモジュールの find_candidates() / update_sale()
などを呼び出すだけで連携できるようにしている。

認証情報について:
    サービスアカウントJSONの中身は一切コードに書かず、次の優先順位で取得する。

    1. Streamlit Community CloudのSecrets
       （st.secrets["gcp_service_account"]。サービスアカウントJSONの各キーを
       .streamlit/secrets.tomlに[gcp_service_account]セクションとして
       設定したもの。JSONファイル自体はGitHubにコミットしない）
    2. ローカル環境の.envで指定したファイルパス（GOOGLE_SERVICE_ACCOUNT_FILE）
       から読み込むサービスアカウントJSONファイル（従来どおり）
"""

import os
import re
import unicodedata
from dataclasses import dataclass

from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# .envファイルから環境変数を読み込む（GOOGLE_SERVICE_ACCOUNT_FILEなど）
load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    # client.open()はスプレッドシート名からファイルを検索するためDrive APIを
    # 使うので、Sheetsのスコープだけでは 403 (insufficient authentication
    # scopes) になる。そのためDriveのスコープも合わせて要求する。
    "https://www.googleapis.com/auth/drive",
]

CREDENTIALS_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "vv古着売上管理")

# スプレッドシート上の列番号（A列=1, B列=2, ...）
COL_MANAGEMENT_NO = 1  # A列: 管理番号
COL_PRODUCT_NAME = 2  # B列: 商品名
COL_SOLD_AT = 16  # P列: 売却日
COL_SOLD_PRICE = 17  # Q列: 売却価格
COL_SHIPPING_FEE = 19  # S列: 配送料（R列は手数料等の自動計算列のため触れない）

HEADER_ROW = 1  # 1行目はヘッダー想定（2行目以降がデータ）

# 特徴語（トークン）として使う最小の文字数。
# 「L」「S」等のサイズ表記1文字は特徴語として扱わない。
MIN_FEATURE_TOKEN_LENGTH = 2

# 特徴語がこの数以上共通していれば同一商品とみなす。
MIN_MATCHED_TOKENS = 2

# 共通する特徴語が1語だけの場合、その語がこの文字数以上あれば
# （＝十分に固有性が高いとみなし）同一商品とみなす。
# 「Tシャツ」のような短い単語1つだけで大量にヒットしないようにするための閾値。
MIN_SOLO_TOKEN_LENGTH = 5

# 空白除去後の全体文字列同士の包含チェックを行う際、どちらか一方が
# この文字数未満なら「短すぎて偶然含まれているだけ」の可能性が高いため
# 包含チェックは行わない（特徴語（トークン）の一致数による判定のみ行う）。
MIN_CONTAINMENT_LENGTH = 5

_client = None  # gspread.Client は初回利用時に作成する（遅延初期化）


@dataclass
class Candidate:
    """B列「商品名」検索でヒットした1件分の情報。"""

    row: int  # スプレッドシート上の行番号（1始まり）
    management_no: str  # A列: 管理番号
    product_name: str  # B列: 商品名


def _get_credentials() -> Credentials:
    """サービスアカウント認証情報を次の優先順位で取得する。

    1. Streamlit Community CloudのSecrets
       （st.secrets["gcp_service_account"]。サービスアカウントJSONの各キーを
       .streamlit/secrets.tomlに[gcp_service_account]セクションとして
       そのまま設定したもの）
    2. ローカル環境の.env / 環境変数（GOOGLE_SERVICE_ACCOUNT_FILE）に
       指定されたサービスアカウントJSONファイル

    streamlitが未インストールの場合や、Streamlit実行環境外（他のスクリプトや
    テストからsheets_client.pyを直接使う場合）、またはSecrets未設定の場合は、
    エラーにせず.envファイル側の読み込みへフォールバックする。

    Raises:
        RuntimeError: どちらの方法でも認証情報が見つからない、または
            指定されたファイルが存在しない場合
    """
    try:
        import streamlit as st

        if "gcp_service_account" in st.secrets:
            info = dict(st.secrets["gcp_service_account"])
            return Credentials.from_service_account_info(info, scopes=SCOPES)
    except Exception:
        # streamlit未インストール／Streamlit実行環境外／secrets.toml未作成など。
        # いずれの場合もエラーにせず.envファイル側の読み込みにフォールバックする。
        pass

    if not CREDENTIALS_FILE:
        raise RuntimeError(
            "Googleサービスアカウントの認証情報が見つかりません。\n"
            "・Streamlit Cloudの場合: Secretsに[gcp_service_account]として"
            "サービスアカウントJSONの内容を設定してください"
            "（.streamlit/secrets.toml.exampleを参考にしてください）。\n"
            "・ローカル環境の場合: .envファイルのGOOGLE_SERVICE_ACCOUNT_FILEに"
            "サービスアカウントJSONファイルのパスを設定してください"
            "（.env.exampleを参考にしてください）。"
        )
    if not os.path.exists(CREDENTIALS_FILE):
        raise RuntimeError(f"認証ファイルが見つかりません: {CREDENTIALS_FILE}")
    return Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)


def _get_client() -> gspread.Client:
    """gspread.Clientを作成（未作成の場合のみ）して返す。"""
    global _client
    if _client is None:
        credentials = _get_credentials()
        _client = gspread.authorize(credentials)
    return _client


def _get_worksheet() -> gspread.Worksheet:
    """対象スプレッドシート（先頭シート）を開いて返す。"""
    client = _get_client()
    try:
        spreadsheet = client.open(SHEET_NAME)
    except gspread.SpreadsheetNotFound as e:
        raise RuntimeError(
            f"スプレッドシート「{SHEET_NAME}」が見つかりません。"
            "サービスアカウントに共有されているか確認してください。"
        ) from e
    return spreadsheet.sheet1


def extract_date(purchased_at: str) -> str:
    """
    Geminiの「購入日時」文字列（例: "2026/8/20 21:34"）から
    日付部分だけを取り出す。

    日付らしき部分が見つからない場合は、元の文字列をそのまま返す。
    """
    match = re.search(r"\d{4}[/\-]\d{1,2}[/\-]\d{1,2}", purchased_at)
    return match.group(0) if match else purchased_at


def extract_number(price_text: str) -> int | None:
    """
    Geminiの「売却価格」文字列（例: "12,000円"）から数値部分を取り出す。

    数字が見つからない場合はNoneを返す。
    """
    digits = re.sub(r"[^\d]", "", price_text)
    return int(digits) if digits else None


def _normalize(text: str) -> str:
    """
    比較用に文字列全体を正規化する（丸ごと比較・包含判定用）。

    - 全角・半角の違いを可能な範囲で吸収する（NFKC正規化）
    - 前後だけでなく文字列中の空白もすべて除去する
    - 大文字・小文字の違いを吸収する（casefold）

    比較専用。表示や書き込みには元の文字列を使うこと。
    """
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"\s+", "", normalized)
    return normalized.casefold()


def _tokenize(text: str) -> list[str]:
    """
    比較用に文字列を特徴語（単語）に分割する。

    全角・半角・大文字小文字を統一した上で空白で区切り、
    「L」「S」等の短すぎる単語（サイズ表記など）は特徴語として使わないため除外する。
    """
    normalized = unicodedata.normalize("NFKC", text).strip().casefold()
    tokens = re.split(r"\s+", normalized) if normalized else []
    return [t for t in tokens if len(t) >= MIN_FEATURE_TOKEN_LENGTH]


def _is_match(title: str, product_name: str) -> bool:
    """
    Geminiの「商品タイトル」とスプレッドシートの「商品名」が同一商品を
    指しているかを判定する。次のいずれかを満たせば一致とみなす。

    1. 空白を除去して正規化した文字列同士で、どちらかがもう一方に含まれる
       （表記ゆれがあっても、片方がもう片方の一部になっているケース。
       ただし、どちらかがMIN_CONTAINMENT_LENGTH文字未満の短い文字列の
       場合は、偶然含まれているだけの可能性が高いため対象外とする）
    2. 特徴語（2文字以上の単語）が2語以上共通している
       （例: 「半袖」「Tシャツ」のように、片方では空白でつながって
       「半袖Tシャツ」という1語になっていても、他の単語が一致していれば
       同一商品とみなせる。B列側に「L」のようなサイズ表記が追加で
       入っていても、それは短い単語として無視されるため一致を妨げない）
    3. 共通する特徴語が1語だけの場合は、その語が十分に長く
       （MIN_SOLO_TOKEN_LENGTH文字以上）固有性が高いとみなせる場合のみ
       一致とみなす（「Tシャツ」のような短い単語1つだけの一致では
       大量の商品がヒットしてしまうのを防ぐため）
    """
    normalized_title = _normalize(title)
    normalized_name = _normalize(product_name)
    if not normalized_title or not normalized_name:
        return False

    if min(len(normalized_title), len(normalized_name)) >= MIN_CONTAINMENT_LENGTH and (
        normalized_name in normalized_title or normalized_title in normalized_name
    ):
        return True

    title_tokens = set(_tokenize(title))
    name_tokens = set(_tokenize(product_name))
    common_tokens = title_tokens & name_tokens

    if len(common_tokens) >= MIN_MATCHED_TOKENS:
        return True
    if len(common_tokens) == 1:
        (only_token,) = common_tokens
        if len(only_token) >= MIN_SOLO_TOKEN_LENGTH:
            return True
    return False


def find_candidates(title: str) -> list[Candidate]:
    """
    B列「商品名」からGeminiの「商品タイトル」と同一商品と判定される
    行を検索し、一覧を返す（判定ロジックは_is_match()を参照）。

    Args:
        title: Geminiが読み取った商品タイトル

    Returns:
        list[Candidate]: 一致した行の一覧（0件の場合は空リスト）
    """
    worksheet = _get_worksheet()
    values = worksheet.get_all_values()

    candidates = []
    for i, row in enumerate(values[HEADER_ROW:], start=HEADER_ROW + 1):
        product_name = row[COL_PRODUCT_NAME - 1] if len(row) >= COL_PRODUCT_NAME else ""
        if _is_match(title, product_name):
            management_no = (
                row[COL_MANAGEMENT_NO - 1] if len(row) >= COL_MANAGEMENT_NO else ""
            )
            candidates.append(
                Candidate(row=i, management_no=management_no, product_name=product_name)
            )
    return candidates


def update_sale(row: int, sold_at: str, sold_price: int, shipping_fee: int) -> None:
    """
    指定行のP列（売却日）・Q列（売却価格）・S列（配送料）だけを更新する。

    P・Q・Sの間にあるR列はスプレッドシート側の自動計算列（手数料等）の
    ため、範囲更新には含めず、P:Qの範囲更新とS単体の更新を分けている。

    販売手数料・利益はスプレッドシート側の数式で自動計算されるため、
    この関数からは一切書き込まない。

    Args:
        row: 更新対象のスプレッドシート上の行番号（1始まり）
        sold_at: P列に書き込む売却日（例: "2026/8/20"）
        sold_price: Q列に書き込む売却価格（数値）
        shipping_fee: S列に書き込む配送料（数値）
    """
    worksheet = _get_worksheet()
    worksheet.batch_update(
        [
            {"range": f"P{row}:Q{row}", "values": [[sold_at, sold_price]]},
            {"range": f"S{row}", "values": [[shipping_fee]]},
        ]
    )
