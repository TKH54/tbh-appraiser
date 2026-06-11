# TBH 倉庫まるごと査定 / TBH Warehouse Appraiser

**Task Bar Hero** の倉庫をワンクリックで読み取り、全アイテムのSteamマーケット相場と
「売り規制前の本当の価値」を表示する非公式ファンメイドツールです。

One click reads your *Task Bar Hero* warehouse and prices every item —
including its REAL pre-freeze market value. Unofficial fan-made tool.

## 使い方 / How to use

1. 「🎥 ゲーム画面に接続」を押して TaskBarHero のウィンドウを選ぶ
2. ゲーム内で倉庫を開き、「🔍 倉庫を査定する」を押す
3. 全アイテムの相場と合計額が表示されます。黄色い「?」のマスだけクリックで確認

対応ブラウザ: デスクトップ版 Chrome / Edge / Firefox（モバイル不可）

## プライバシー / Privacy

- 🔒 **画面キャプチャはすべてブラウザ内で処理され、どこにも送信されません。**
- ログイン・アカウント連携は一切ありません。
- 訂正の学習データはあなたのブラウザ内（localStorage）にのみ保存されます。
- Screenshots are processed entirely in your browser and never uploaded.

## 仕組み / How it works

- 認識: 倉庫グリッドを検出し、アイテム画像をカタログ＋学習済みラベルと照合（純JavaScript・約10ms/マス）
- 現在価格: GitHub Actions が約10分ごとに更新する静的スナップショット（中央値・24h販売数は毎時）
- 売り規制前の価格: 規制前（2026/6/2〜6/8）にSteamで実際に売れた価格の数量加重平均（不変の過去データ）
- アイテム画像は Steam CDN から直接表示しています（再配布していません）

## 免責 / Disclaimer

- 本ツールは非公式です。Task Bar Hero および Steam の商標・ゲーム内アセットの権利は各権利者に帰属します。
- 価格情報の正確性・完全性は保証されません。取引は自己責任でお願いします。
- ゲームへの干渉（メモリ改変・自動操作など）は一切行いません（読み取り専用）。
- 権利者からの要請があれば速やかに対応します。
- This tool is read-only and never interferes with the game. Will promptly comply with any takedown request from rights holders.

## License

Code: MIT. Game-derived data (item names, drop rates) and Steam Market data
belong to their respective owners and are included for interoperability only.
