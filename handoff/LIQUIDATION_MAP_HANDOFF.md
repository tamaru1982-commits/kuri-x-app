# 引き継ぎ資料: 清算マップ(手動メモ)機能の追加

このドキュメントは、既存の`HANDOFF.md`に対する追加分です。
清算マップ機能についてのみ、実装方針をまとめています。

## 1. 背景

暗号資産の「清算マップ(Liquidation Heatmap)」は、レバレッジポジションの
強制決済が集中しやすい価格帯を推定したデータで、CoinGlass(coinglass.com)
というサービスで見られる。

- **無料**: Webサイトでの目視閲覧のみ
- **有料($29/月〜)**: API経由での自動取得

現在の運用資金規模(少額からのコツコツ運用)では月$29の課金は割に合わないため、
**API連携による完全自動化は見送り、手動確認+メモ記録という折衷案**を採用する。

## 2. 運用フロー(すでに実践済み)

```
1. ユーザーがcoinglass.comの清算マップ(BTC等)をブラウザで開く
   URL例: https://www.coinglass.com/pro/futures/LiquidationHeatMap?coin=BTC&type=symbol
2. スクリーンショットを撮る
3. Claudeのチャット(claude.ai / スマホアプリ)に画像を貼って解析してもらう
   (色の濃淡から、どの価格帯に清算が集中しているかを言語化してもらえる)
4. 気づいた内容(価格帯・方向性)を、システムに手動で記録する ← ここを実装したい
```

## 3. 実装したい機能: 清算メモの記録・表示

### 3-1. DBスキーマ追加(db_utils.pyに追加)

```python
CREATE TABLE IF NOT EXISTS liquidation_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,       -- 記録した日時
    asset TEXT NOT NULL,           -- 例: BTC
    price REAL NOT NULL,           -- 壁があると判断した価格
    direction TEXT,                -- 'ロング清算の壁' | 'ショート清算の壁' 等(自由記述でも可)
    note TEXT,                     -- 補足メモ(自由記述)
    valid_until TEXT               -- この情報がいつまで有効そうか(任意、NULL可)
)
```

既存の`init_db()`関数に上記のCREATE TABLE文を追加する形で対応可能。

### 3-2. 記録用CLIスクリプト: `liquidation_note_add.py`

`journal_add.py`と同じ設計思想(argparseでコマンドライン引数を受け取る)で作成する。

```bash
python liquidation_note_add.py --asset BTC --price 78600 --direction "ロング清算の壁" --note "現在価格のすぐ下、一番濃い帯"
```

実装イメージ(journal_add.pyを参考に):
```python
import argparse
import db_utils
from datetime import datetime

def add_liquidation_note(asset, price, direction, note):
    conn = db_utils.get_conn()
    conn.execute(
        "INSERT INTO liquidation_notes (timestamp, asset, price, direction, note) VALUES (?, ?, ?, ?, ?)",
        (datetime.utcnow().isoformat(), asset, price, direction, note)
    )
    conn.commit()
    conn.close()

# argparseで --asset --price --direction --note を受け取り、上記関数を呼ぶ
```

### 3-3. 古いメモの自動整理(任意)

清算マップは相場が動くと数時間〜1日程度で情報が古くなるため、
一定期間(例: 48時間)経過したメモは、ダッシュボード表示から除外する
(削除はせず、表示フィルタのみでよい)ロジックをどこかに入れる。

### 3-4. ダッシュボードへの表示

現在使っているダッシュボード生成処理(GitHub Pagesで公開しているもの)に、
各銘柄の表示の下に「清算メモ」セクションを追加する。

表示イメージ:
```
🔴 BTC: SHORT | 価格 $78,953

  ⚠️ 清算メモ(手動記録, 2時間前)
  　$78,600: ロング清算の壁(現在価格のすぐ下、要警戒)
```

直近48時間以内のメモのみ、該当銘柄の現在価格に近いもの(例: ±5%以内)を
優先的に表示するとよい。

## 4. 明確な非対象(やらないこと)

- **`confluence_checker.py`などの自動判定ロジックには組み込まない**
  (清算メモはあくまで人間の判断材料であり、自動売買や自動シグナル生成の
  入力にはしない。誤った手動記録が自動判定に混入するのを避けるため)
- **API連携による自動取得は現時点では見送り**(コスト面で運用規模に見合わない)
- スクリーンショットの解析自体の自動化(画像認識)も現時点では不要
  (Claudeとの対話で都度解析すれば十分)

## 5. 実装の優先度

低〜中程度。すでに手動運用(スクショ→チャットで解析)は機能しているため、
「記録が残らず、後から振り返れない」という不便さを解消する程度の位置づけ。
他の未実装項目(risk_utilsの簡略化、クジラ監視)の方が優先度は高い。
