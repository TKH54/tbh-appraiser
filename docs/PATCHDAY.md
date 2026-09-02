# パッチ当日ランブック（新アイテム追加時の対応手順）

**ゲームに新アイテムが追加された日**にやることの手順書。
(2026-07-12 作成 / 2026-09-02 全面改訂: カタログ追加が autocatalog で自動化されたため)

次のパッチ: **2026-09-08 (火) 14:00 JST — Plaguelands**
（新ステージ / Lv90装備 / 新ユニークステータス約20種 / 新ルーン / Corrosion レシピ /
Corrupted Soulstone / スキン / 統計メニュー）

---

## 自動で追従するもの（何もしなくていい）

- **価格**: 価格bot の sweep が Steam マーケット全体を舐めるので、新アイテムも出品された
  瞬間から prices.json に入り、相場表・出品プランに出る。
- **カタログと認識データ**: `autocatalog.yml`（6時間おき）が「出品されているのに
  items.json に無い」アイテムを拾い、Steam から アイコンを取得して items.json に追加、
  新しいアートワークなら refs.bin に ref を追記する。
  - 既存アイコンの新グレード → 即反映
  - 新規アイコン → `seed_regression` で確信誤認識が増えないと確認できた時だけ反映。
    増えた/測れない場合は **PR を立てて Discord 通知**（自動では入らない）
  - 危険な形（後述の「保留されるもの」）→ 反映せず Discord 通知のみ
- **上位3グレード解禁**: unlocked3 検知が Discord 通知し、ガチャEVの除外も自動解除
  （誤検知時は `gh workflow run prices.yml -f force_unlocked3=0`）。
- **認識ラベル**: crowd labels が貯まり、毎週の labels.yml が自動昇格する。

急ぎたい時は手動起動できる: `gh workflow run autocatalog.yml`
（下見だけなら `-f dry_run=true`）

---

## 手動でやること

### 1. 日本語名の抽出（唯一、PCが必須の作業）

**新しいベース**の日本語名だけは、ゲームの Unity 文字列テーブルからしか取れない。
それまで該当アイテムは英語の市場名＋「未翻訳」バッジで表示される（実害はないが見栄えが悪い）。

Steam でゲーム本体がパッチ更新された後、`Desktop/TBH/tbh-market-monitor` で:

```
python localize.py          # 要 UnityPy。バンドルが見つからない時はパスを引数で渡す
```

生成された `assets/ja_names.json` を `tbh-appraiser-site/data/ja_names.json` に**このファイルだけ**
コピーし、`python scripts/bump_release.py` してから push。

既に取り込まれたアイテムの `name_ja` は次の autocatalog 実行では埋め直されない
（既存エントリは触らない設計）。まとめて直すなら該当アイテムを items.json から消して
autocatalog に拾い直させるのが早い。

### 2. 「保留」通知が来たら判断する

autocatalog は、自動で入れると**誤認識を生む形**を3つ検出して保留し、Discord に通知する。
1日1回だけ鳴る（対応するまで残るので、6時間おきに鳴らすと無視するようになるため）。

- **既存素材と同名ベースのグレード品** — `pipeline.js` はベースの variant が1件で
  rarity が空のときだけ material 判定し、それが自動確定の厳格バー(0.05)を効かせている。
  グレード品を足すとそのバーが外れる。
- **既知アイコンを新ベースで再利用** — アイコン↔ベースは全302件で1:1。崩すと新アイテムが
  既存アイテムとして誤認識される。
- **新規アイコンを2つの新ベースが共有** — 同一ベクトルで別ベースの ref が2本になり、
  マッチャの1位が運任せになる。

いずれも `refs.bin` を動かさないので回帰ゲートでは捕まえられない。**人間が判断する前提**。
対応するなら手元で items.json / refs.json / refs.bin を編集して PR にする。

### 3. ガチャ排出率（開発側が祈願テーブルを変えた時だけ）

`gacha.json` は datamine 値。変更告知が出たら probonk の元データを取り直し、
`tbh-market-monitor` 側を更新 → `build_web_data.py` → **gacha.json だけ**コピー → bump。
それまでガチャEVパネルは旧レートなので、高額コインの判断には使わせない。

---

## ⚠ やってはいけないこと

**`build_web_data.py` の出力を `data/` に丸ごと上書きコピーしない。**
2026-09-02 より前の手順ではそうしていたが、いまは壊れる:

- `refs.bin` / `refs.json` — autocatalog が回帰ゲートを通して**追記**したものを捨ててしまう。
  ローカルの sprite キャッシュから作り直すと、その追記分は消える。
- `items.json` — 同上。autocatalog が入れたエントリが消え、次の実行で入れ直しになる。
- `learned_seed.json` — crowd-label 昇格の成果物。ローカルは必ず古い。上書きすると
  サイトの認識精度が巻き戻る。
- `prices.json` / `history.json` / `price_state.json` — 価格botの持ち物。

コピーしていいのは **`ja_names.json` と `gacha.json` の2つだけ**。
`catalog.py` はローカルの sprite キャッシュ更新には使ってよいが、その結果をサイトに
持っていく必要はもう無い。

---

## 事故ったとき

**入った ref がやっぱり悪かった場合**、専用のロールバックは無い（`rollback.yml` は
learned_seed 専用）。`git revert` で戻す。1回の実行が **tier1 と tier2 で最大2コミット**を
作るので、戻したい方（または両方）を選ぶ:

```
git log --oneline --grep="^autocatalog:" -5
git revert <sha>                  # refs を戻すなら "ref(s) for new artwork" の方
python scripts/bump_release.py    # revert で APP_VERSION も巻き戻るので進め直す
git push
```

`refs.bin` は固定4096バイト刻みの追記なので、revert しても既存 ref のインデックスは動かない。

**autocatalog が暴走していると思ったら**、まず止める:
```
gh workflow disable autocatalog.yml
```

---

## 動作確認

- サイトを開いて新アイテムが相場表に出るか（価格は sweep 1周 = 最大~1時間で揃う）
- 新アイテムをスキャンして認識されるか。**認識されず [?] なのは正常**
  （誤IDより [?] が正 = 誤認識ゼロ方針）
- `_autocatalog_report.json` は実行ごとの作業記録（リポジトリには入らない）。
  中身は Actions のログで見る。
