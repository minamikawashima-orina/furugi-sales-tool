"""
古着販売作業効率化ツール（MVP）

メルカリのスクリーンショットをアップロードすると、Gemini APIが
商品情報（ブランド・ジャンル・価格など）を自動で読み取って画面に表示する。

さらに、読み取った「商品タイトル」でGoogleスプレッドシート（B列「商品名」）
を検索し、一致した行のA列「管理番号」を特定した上で、確認画面を経て
P列「売却日」・Q列「売却価格」・S列「配送料」を書き込む。

販売手数料・利益はスプレッドシート側の数式で自動計算されるため、
このツールからは一切書き込まない。

今回のMVPでは以下は実装しない:
    - Google Drive連携
    - 自動出品
"""

import streamlit as st

from gemini_client import analyze_mercari_image
import sheets_client

st.set_page_config(page_title="古着販売作業効率化ツール", page_icon="👕")

st.title("👕 古着販売作業効率化ツール（MVP）")
st.write(
    "メルカリの商品ページのスクリーンショットをアップロードすると、"
    "Gemini APIが商品情報を自動で読み取ります。"
)

uploaded_file = st.file_uploader(
    "メルカリのスクリーンショットを選択してください",
    type=["png", "jpg", "jpeg"],
)

if uploaded_file is not None:
    image_bytes = uploaded_file.getvalue()

    # 1. アップロードした画像を表示
    st.image(image_bytes, caption="アップロードされた画像", use_container_width=True)

    # 2. ボタンが押されたらGemini APIで解析
    if st.button("この画像を解析する"):
        with st.spinner("Gemini APIで画像を解析中..."):
            try:
                result = analyze_mercari_image(image_bytes, uploaded_file.type)
            except Exception as e:
                st.error(f"解析中にエラーが発生しました: {e}")
                st.session_state.pop("analysis_result", None)
            else:
                st.session_state["analysis_result"] = result
                # 新しく解析し直した場合、前回のスプレッドシート検索結果は破棄する
                st.session_state.pop("sheet_candidates", None)
                st.session_state.pop("sheet_error", None)

    result = st.session_state.get("analysis_result")

    if result is not None:
        # 3. 解析結果を画面に表示
        st.success("解析が完了しました！")
        st.subheader("解析結果")
        st.table([{"項目": key, "値": value} for key, value in result.items()])

        # 4. Googleスプレッドシートから対象商品（B列「商品名」）を検索する
        #    （同じ解析結果に対しては1回だけ検索する）
        if "sheet_candidates" not in st.session_state:
            with st.spinner("スプレッドシートで対象商品を検索中..."):
                try:
                    candidates = sheets_client.find_candidates(result.get("商品タイトル", ""))
                except Exception as e:
                    st.session_state["sheet_candidates"] = None
                    st.session_state["sheet_error"] = str(e)
                else:
                    st.session_state["sheet_candidates"] = candidates
                    st.session_state["sheet_error"] = None

        st.subheader("スプレッドシートへの登録")

        sheet_error = st.session_state.get("sheet_error")
        candidates = st.session_state.get("sheet_candidates")

        if sheet_error:
            # 検索そのものが失敗した場合（認証エラー・シートが見つからない等）
            st.error(f"スプレッドシートの検索中にエラーが発生しました: {sheet_error}")
        elif not candidates:
            # 7. B列に一致する商品がない場合は登録せず、エラーを表示する
            st.error(
                "スプレッドシート上に一致する商品が見つかりませんでした。"
                "商品名（B列）を確認してください。"
            )
        else:
            if len(candidates) == 1:
                selected = candidates[0]
            else:
                # 8. 複数の商品が一致した場合は候補を表示し、選んで確認できるようにする
                st.warning(
                    f"{len(candidates)}件の商品が一致しました。登録する商品を選択してください。"
                )
                options = {
                    f"管理番号: {c.management_no} / 商品名: {c.product_name}": c
                    for c in candidates
                }
                choice = st.radio("対象商品を選択してください", list(options.keys()))
                selected = options[choice]

            sold_at = sheets_client.extract_date(result.get("購入日時", ""))
            sold_price = sheets_client.extract_number(result.get("売却価格", ""))
            shipping_fee = sheets_client.extract_number(result.get("配送料", ""))

            # 11. 登録前に、選択した内容を画面に表示する
            st.write("以下の内容でスプレッドシートに登録します。")
            st.table(
                [
                    {"項目": "管理番号（A列）", "値": selected.management_no},
                    {"項目": "商品名（B列）", "値": selected.product_name},
                    {"項目": "売却日（P列）", "値": sold_at},
                    {"項目": "売却価格（Q列）", "値": sold_price},
                    {"項目": "配送料（S列）", "値": shipping_fee},
                ]
            )

            if sold_price is None:
                st.error("売却価格から数値を読み取れませんでした。登録できません。")
            elif shipping_fee is None:
                st.error("配送料から数値を読み取れませんでした。登録できません。")
            # 12. 「登録する」ボタンを押したときだけスプレッドシートを更新する
            elif st.button("登録する"):
                try:
                    sheets_client.update_sale(selected.row, sold_at, sold_price, shipping_fee)
                except Exception as e:
                    st.error(f"スプレッドシートへの登録中にエラーが発生しました: {e}")
                else:
                    st.success(f"管理番号「{selected.management_no}」の行に登録しました！")
