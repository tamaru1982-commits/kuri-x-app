# 仮想通貨 ロング/ショート シグナル通知アプリ

CoinGecko の無料パブリックAPI（APIキー・登録不要）から価格を取得し、
移動平均クロス + RSI というシンプルなテクニカル指標で LONG / SHORT / 様子見 を判定して、
Discord に自動通知します。**自動売買は行いません（通知のみ）。**

## ファイル構成

- `crypto_signal_notifier.py` … メインロジック
- `requirements.txt` … 依存パッケージ
- `crypto_signal.yml` … GitHub Actions 用の定期実行設定（`.github/workflows/` に配置）

## セットアップ手順

### 1. Discord Webhook を作る（通知先）

1. 通知を受け取りたい Discord サーバー／チャンネルを用意（自分だけのサーバーでOK）
2. チャンネル設定 → 「連携サービス」→「ウェブフック」→「新しいウェブフック」
3. 表示されたWebhook URLをコピーしておく

### 2. GitHubリポジトリを作る（PCを常時起動しなくてよい方式）

1. GitHubで新規リポジトリを作成（Private推奨）
2. `crypto_signal_notifier.py` と `requirements.txt` をリポジトリ直下にアップロード
3. `crypto_signal.yml` を `.github/workflows/crypto_signal.yml` という場所に配置
4. リポジトリの `Settings` → `Secrets and variables` → `Actions` → `New repository secret`
   - Name: `DISCORD_WEBHOOK_URL`
   - Value: 手順1でコピーしたWebhook URL
5. `Actions` タブを開き、ワークフローを一度手動実行（`Run workflow`）して動作確認

これで **30分ごと（cronの設定で変更可）に自動実行**され、Discordに通知が届きます。
PCを閉じていてもGitHub側で実行されるので、常時稼働のPCやサーバーは不要です。

### 3. ローカルで試したい場合

```bash
pip install -r requirements.txt
export DISCORD_WEBHOOK_URL="あなたのWebhook URL"
python crypto_signal_notifier.py
```

## カスタマイズポイント

`crypto_signal_notifier.py` 内の以下を編集することで調整できます:

- `COINS`: 監視したい銘柄を追加・削除（CoinGeckoのcoin idを指定）
- `SMA_SHORT` / `SMA_LONG`: 移動平均の期間
- `RSI_PERIOD` / `RSI_OVERBOUGHT` / `RSI_OVERSOLD`: RSIの判定閾値
- `crypto_signal.yml` の `cron`: 通知頻度（現在は30分おき）

## 注意事項

- CoinGecko無料APIには利用制限があります（多数銘柄・高頻度実行時は注意）
- このシグナルは単純なテクニカル指標に基づくものであり、将来の値動きを保証するものではありません
- 投資判断は自己責任で行ってください

---

## 追加機能: 特定Xアカウントの投稿からのシグナル通知

`x_signal_notifier.py` / `x_signal.yml` を使うと、特定のXアカウントの投稿を監視し、
キーワードにマッチしたら LONG / SHORT のサインとしてDiscordに通知できます。

### 前提: X API利用について

2026年2月からX APIは従量課金制（Pay-Per-Use）になり、無料枠は基本的にありません。
- 投稿の読み取り: 約$0.005/件
- 新規登録者には$10分のクレジットが付与される
- 個人が1アカウントを低頻度（15分おき程度）で監視する用途なら、月額は数ドル程度に収まります
- 公式APIのみを使用し、スクレイピングは行いません（規約違反・アカウント凍結リスクを避けるため）

### セットアップ手順

1. **X Developer Portal** (developer.x.com) でアプリを作成し、Bearer Tokenを取得
2. `x_signal_notifier.py` 内の `TARGET_USERNAME` を監視したいアカウント名に変更
3. 必要に応じて `LONG_KEYWORDS` / `SHORT_KEYWORDS` を編集（デフォルトは日本語+英語の簡易キーワード）
4. `x_signal_notifier.py` をGitHubリポジトリ直下にアップロード
5. `x_signal.yml` を `.github/workflows/x_signal.yml` に配置
6. リポジトリのSecretsに `X_BEARER_TOKEN` を追加（`DISCORD_WEBHOOK_URL` は既存のものを共用可）
7. `Settings > Actions > General > Workflow permissions` を **「Read and write permissions」** に変更
   - 新着投稿の重複通知を防ぐため、最後に確認した投稿IDをリポジトリに自動コミットする仕組みのために必要です
8. Actionsタブから手動実行して動作確認

### カスタマイズポイント

- `TARGET_USERNAME`: 監視対象のアカウント
- `LONG_KEYWORDS` / `SHORT_KEYWORDS`: 判定に使うキーワード
- `x_signal.yml` の `cron`: チェック頻度（現在は15分おき。API利用料と相談して調整してください）

### 注意事項

- キーワードマッチによる単純な判定です。皮肉・引用・否定文（例:「買いではない」）なども拾ってしまう可能性があります
- 投稿内容が実際にその人の見解を正確に反映しているとは限りません
- 投資助言ではありません

---

## 追加機能: 米雇用統計(NFP)の過去パターン・リマインド通知

`nfp_pattern_analysis.py`（過去パターンの集計） / `nfp_reminder.py`（前日リマインド） / `nfp_reminder.yml`
を使うと、雇用統計発表前日に「過去はこういう値動きの傾向があった」という参考情報をDiscordに通知できます。

**重要**: 「次回の発表がどうなるか」を予測する機能ではありません。あくまで過去の統計を通知するものです。

### 前提: FRED APIについて

FRED（セントルイス連邦準備銀行が運営する経済データベース）は**完全無料**で、APIキーもその場で即発行されます。
1. https://fred.stlouisfed.org/ でアカウント作成
2. マイページの「API Keys」からキーを発行（審査なし、即時発行）

### セットアップ手順

1. FRED APIキーを取得
2. `nfp_pattern_analysis.py` をローカル（PC）で一度実行し、`nfp_patterns.json` を生成
   ```bash
   pip install -r requirements.txt
   export FRED_API_KEY="あなたのFREDキー"
   python nfp_pattern_analysis.py
   ```
3. 生成された `nfp_patterns.json` を含め、`nfp_reminder.py` と一緒にGitHubリポジトリにアップロード
4. `nfp_reminder.yml` を `.github/workflows/nfp_reminder.yml` に配置
5. Secretsに `FRED_API_KEY` を追加（`DISCORD_WEBHOOK_URL` は既存のものを共用可）
6. Workflow permissionsを「Read and write permissions」に変更
7. Actionsタブから手動実行して動作確認

### 運用のポイント

- `nfp_patterns.json` は**データが古くなるので、月1回程度 `nfp_pattern_analysis.py` を再実行して更新**するのがおすすめです（ローカルで実行し、更新後のファイルをリポジトリにpushするだけです）
- `TARGETS`（分析対象の資産）は `nfp_pattern_analysis.py` 内で自由に編集できます。yfinanceのティッカーコードで指定します

### 注意事項

- 発表日と実績データの対応付けは近似的な方法です（詳細はスクリプト内コメント参照）
- あくまで過去の統計的傾向であり、将来の値動きを保証するものではありません
- 投資助言ではありません

---

## フェーズ2: 分析・リスク管理機能一式

利益を出すための運用面を補強する機能群です。全て `db_utils.py` が管理する
共有データベース `trading_system.db` を中心に連携しています。

### 追加ファイル一覧

| ファイル | 役割 |
|---|---|
| `db_utils.py` | 共有DB(シグナル履歴・トレード日誌)。他の全スクリプトが利用 |
| `risk_utils.py` | ポジションサイズ・損切りラインの計算(crypto_signal_notifier.pyに統合済み) |
| `accuracy_tracker.py` + `accuracy_tracker.yml` | シグナルの的中率を自動検証し、月曜に週次レポート送信 |
| `confluence_checker.py` + `confluence_checker.yml` | 複数ソースのシグナルが重なったら強調通知 |
| `economic_calendar_reminder.py` + `economic_calendar_reminder.yml` | NFP/CPI/PCE(+設定すればFOMC)の発表日リマインド(nfp_reminder.pyの後継) |
| `correlation_report.py` + `correlation_report.yml` | 追跡資産間の相関を週次で通知 |
| `journal_add.py` / `journal_close.py` / `journal_report.py` | 手動トレード記録・成績集計(ローカルで実行) |

`crypto_signal_notifier.py` と `x_signal_notifier.py` も、シグナルをDBに記録し、
クールダウン(同一資産・同一方向の再通知を一定時間抑制)する形に更新されています。

### セットアップ手順(追加分)

1. リポジトリ直下に上記の新規ファイルを全てアップロード
2. `.github/workflows/` に各 `.yml` ファイルを配置
3. Secretsは既存のもの(`DISCORD_WEBHOOK_URL`, `X_BEARER_TOKEN`, `FRED_API_KEY`)を共用可能。追加は不要
4. 環境変数で以下を調整可能(未設定でもデフォルト値で動作します)
   - `RISK_PERCENT_PER_TRADE`: 1トレードあたりのリスク許容%(デフォルト1.0)
   - `DEFAULT_STOP_LOSS_PCT`: 損切りまでの値幅%(デフォルト3.0)
   - `CRYPTO_COOLDOWN_MINUTES` / `X_COOLDOWN_MINUTES` / `CONFLUENCE_COOLDOWN_MINUTES`: 各クールダウン時間(分)
   - これらはGitHub Secretsまたはワークフローのenvに追加してください(値を空のまま追加しないこと。未設定の場合は追加しないでください)
   - `risk_utils.py`はポジション金額・数量の計算は行わず、損切りライン(%)と
     「1トレードの損失許容は口座資金の◯%まで」という通貨非依存の目安のみを通知します。
     実際の数量・金額は口座資金(円)とその時のレートに応じて各自で計算してください

### 各機能の実行タイミング(初期設定)

- crypto_signal_notifier: 30分おき
- x_signal_notifier: 15分おき
- confluence_checker: 毎時10分・40分(元の通知の少し後)
- accuracy_tracker: 毎時5分(結果検証)、月曜に週次サマリー送信
- economic_calendar_reminder: 毎日1回
- correlation_report: 毎週日曜

### トレード日誌の使い方(ローカル実行)

```bash
# エントリー時
python journal_add.py --asset BTC --direction LONG --entry 65000 --size 0.1 --stop 63000 --note "コンフルエンス+SMAクロス"

# 決済時(journal_add.pyの出力に表示されたIDを使う)
python journal_close.py --id 1 --exit 67000

# 成績サマリーを見る
python journal_report.py
```

トレード記録も `trading_system.db` に保存されるため、リポジトリにpushしておけば
どのPCからでも同じ履歴を参照できます(手動でgit push/pullする必要があります)。

### 移行に関する注意

- `nfp_reminder.py` / `nfp_reminder.yml` は `economic_calendar_reminder.py` に統合されました。
  新規セットアップの場合は `economic_calendar_reminder.py` 系を使うことを推奨します
  (`nfp_pattern_analysis.py` はNFPの過去パターン生成用として引き続き使用します)
- `FOMC_MEETING_DATES` は自動取得できないため、`economic_calendar_reminder.py` 内に手動で追記してください
  (公式スケジュール: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm )

---

## フェーズ3: クジラ(大口)監視

`whale_signal_notifier.py` + `whale_signal.yml` を使うと、大口の暗号資産移動を
自動検知して投稿するXアカウント(`@whale_alert`)の投稿を監視し、
「取引所への入金/出金」をLONG/SHORTのサインとしてDiscordに通知できます。

### 判定ロジック(簡易的な経験則)

- BTC/ETH等の主要資産が取引所へ入金された → 売り圧力の可能性(SHORT)
- ステーブルコイン(USDT/USDC等)が取引所へ入金された → 買い準備の可能性(LONG)
- 資産が取引所から出金された → 長期保有の意思表示(LONG)

`db_utils.py`に`source="whale_flow"`として記録され、`confluence_checker.py`が
テクニカル・X投稿・クジラの一致も自動的に検知します。

### セットアップ手順

1. `whale_signal_notifier.py`をリポジトリ直下にアップロード
2. `whale_signal.yml`を`.github/workflows/whale_signal.yml`に配置
3. Secretsは既存のもの(`X_BEARER_TOKEN`, `DISCORD_WEBHOOK_URL`)を共用可能。追加は不要

### 注意事項

- Whale Alertの投稿フォーマットへの単純なパターンマッチによる判定です。
  投稿フォーマットが変更されると抽出できなくなる可能性があります
- 大口移動は取引所の内部振替(コールドウォレット間移動など)の場合もあり、
  必ずしも売買意図を意味しません。投資助言ではありません

---

## フェーズ4: ステータスダッシュボード

`dashboard_generator.py` + `dashboard.yml` を使うと、直近のシグナル・的中率・
保有中ポジション・トレード成績を1枚のHTMLにまとめてGitHub Pagesで公開できます。
Discordの通知が来た瞬間だけでなく、いつでも「今どうなっているか」を確認できます。

### セットアップ手順

1. `dashboard_generator.py`をリポジトリ直下にアップロード
2. `dashboard.yml`を`.github/workflows/dashboard.yml`に配置(15分おきに`docs/index.html`を自動更新)
3. リポジトリの`Settings > Pages`で、Source: `Deploy from a branch`、Branch: `main` / `/docs` を選択
4. 発行されたURLをスマホのホーム画面に追加すれば、簡易アプリのように使えます

### 注意事項

- **GitHub FreeプランのPrivateリポジトリでは、Pagesで公開したURLは「知っていれば誰でも閲覧できる」状態になります**
  (検索エンジンには載りませんが、真の意味での非公開ではありません)。トレード記録を含むため、
  URLを第三者に共有しないよう注意してください。より強い非公開性が必要な場合はGitHub Pro/Team以上の
  「Private Pages」機能が必要です

### 全体としての位置づけ・限界

- これは「判断材料を増やし、記録を残し、リスクを可視化する」ためのツール群であり、
  利益を保証するものではありません
- 的中率(accuracy_tracker)は方向のみの単純な判定です。値幅・手数料・スリッページは考慮していません
- リスク計算(risk_utils)は固定比率モデルの参考値であり、実際の資金管理はご自身の判断で行ってください
- 自動売買機能は引き続き搭載していません(全て通知のみ)
