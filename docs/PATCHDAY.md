# パッチ当日ランブック（新アイテム追加時の対応手順）

Q3 の Plaguelands / Lv90 装備など、**ゲームに新アイテムが追加された日**にやることの手順書。
(2026-07-12 作成。ロードマップ: Q3=Plaguelands/Lv90装備、Q4=Season1 Chaos)

## 自動で追従するもの（何もしなくていい）

- **価格**: 価格bot の sweep は Steam マーケットの全アイテムを舐めるので、新アイテムも
  出品された瞬間から自動で prices.json に入り、サイトの相場表・出品プランに出る。
- **上位3グレード解禁**: unlocked3 検知が Discord に通知し、ガチャEVの除外も自動解除
  （誤検知時は `gh workflow run prices.yml -f force_unlocked3=0`）。
- **認識ラベル**: 新アイテムは当初 [?] 表示（誤認識ゼロ方針どおり）。ユーザーの確定操作で
  crowd labels が貯まり、毎週の labels.yml が自動昇格 → 数日〜1週間で認識されるようになる。

## 手動でやること（この順で、所要 ~30分）

作業場所: `Desktop/TBH/tbh-market-monitor`（データ生成側）→ `Desktop/TBH/tbh-appraiser-site`（サイト側）

1. **カタログ再取得**（新アイテムがマーケットに出品され始めてから）
   ```
   python catalog.py
   ```
   → Steam から全アイテム＋新スプライトを取得し assets/catalog.json を更新。

2. **日本語名の抽出**（Steam でゲーム本体がパッチ更新された後）
   ```
   python localize.py
   ```
   → ゲームの localization バンドル（自動検出）から assets/ja_names.json を再生成。
   UnityPy が要る（`pip install UnityPy`）。バンドルが見つからない時はパスを引数で渡す。

3. **サイト用データ一式を生成**
   ```
   python build_web_data.py
   ```
   → `web/data/` に items.json / refs.bin / refs.json / ja_names.json / meta.json 等を出力。

4. **サイトリポジトリへコピー**
   `web/data/` の生成物を `tbh-appraiser-site/data/` に上書き。**ただし以下は絶対に上書きしない**:
   - `prices.json` / `history.json` / `price_state.json`（価格botの持ち物）
   - `learned_seed.json`（crowd-label 昇格の成果物。ローカルの LEARNED_DIR は古い可能性が
     高い。上書きするとサイトの認識精度が巻き戻る）

5. **リリース**
   ```
   python scripts/bump_release.py
   ```
   （APP_VERSION とキャッシュバスターを両方進める。手動編集はしない）
   - 変更履歴（app.js の CHANGES）の文面は **公開前に必ずユーザー確認**（既定ルール）。
   - push は価格botが10分毎にコミットするので、fetch→自分の変更を載せ直し→push を
     リトライする（普通の rebase だと prices.json が衝突する。生成物は常に「作り直して
     上に載せる」）。

6. **動作確認**
   - サイトを開いて新アイテムが相場表に出るか（価格は sweep 1周 = 最大~1時間で揃う）
   - 新アイテムをスキャンして [?] 表示になるか（誤IDより [?] が正）

## ガチャ排出率の更新（開発側のバグ修正パッチが来た時）

祈願の排出率テーブル（gacha.json）は datamine 値で、2026-06-18 の「コイン未満のグレードが
出るバグ」修正**後**は現行レートが全部変わる。修正パッチが来たら:
1. probonk の gacha.json（datamine 元）を再取得して `tbh-market-monitor` 側の元データを更新
2. `build_web_data.py` → コピー → bump（上の手順 3〜5）
3. それまでガチャEVパネルの数値は「修正前レート」なので、高額コインの判断には使わせない
