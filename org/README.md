# org/ — AI開発組織の配布物

このディレクトリは、**開発対象リポジトリへ配置する組織の実行時ファイル**である。

本リポジトリ（`agents-org`）は組織の**定義**を持つ場所であり、組織が実際に動くのは開発対象リポジトリの側である。したがってここのファイルは、本リポジトリのルート `.claude/` へは置かない。理由は `docs/decisions/13-execution-form.md` の B を参照。

## 中身

| ファイル | 配置先（開発対象リポジトリ） | 内容 |
| --- | --- | --- |
| `CLAUDE.md` | リポジトリのルート | 層2の運営規約。組織の構成、標準フロー、共通規約、Git、直接通信 |
| `glossary.md` | `docs/` | **用語集。** 一般的な意味と違う語、範囲が曖昧になる語、このプロジェクトの語。最後の節はプロジェクト開始後にオーケストレーターが育てる |
| `rules/org-task-ledger.md` | `.claude/rules/` | タスク台帳の書き方（CSV索引 + タスク別Markdown） |
| `rules/org-escalation.md` | `.claude/rules/` | エスカレーションの4段階、共通停止条件、PO確認待ちキュー |
| `agents/org-design.md` | `.claude/agents/` | 設計エージェント |
| `agents/org-implementation.md` | `.claude/agents/` | 実装エージェント |
| `agents/org-review.md` | `.claude/agents/` | レビューエージェント（**編集系ツールを持たない**） |
| `agents/org-test.md` | `.claude/agents/` | テストエージェント |
| `agents/org-documentation.md` | `.claude/agents/` | ドキュメントエージェント |
| `skills/org-orchestrate/` | `.claude/skills/` | オーケストレーターの手順 |
| `skills/org-session-resume/` | `.claude/skills/` | セッション再開の手順 |
| `skills/org-first-run-check/` | `.claude/skills/` | **初回運用の点検手順。** 最初のタスクが1件完了したら使う |
| `templates/T-XXX.md` | `.claude/templates/` | **タスク別ファイルの雛形。** コピーして使う |
| `templates/task-list.csv` | `.claude/templates/`（使うときに `docs/task-list-{project-name}.csv` へ複写） | 索引のヘッダ行だけの空ファイル |
| `templates/po-queue.md` | `.claude/templates/`（使うときに `docs/po-queue.md` へ複写） | **PO確認待ちキューの雛形。** 見出しの書式が検査対象 |
| `templates/tasks-README.md` | `.claude/templates/`（使うときに `docs/tasks/README.md` へ複写） | 詳細ファイルの置き場の説明 |
| `scripts/org-check.py` | `.claude/scripts/` | **停滞検知と整合性検査。** Python 3.8 以降、標準ライブラリのみ |
| `scripts/org-tokens.py` | `.claude/scripts/` | **トークン消費の集計。** タスク別・担当エージェント別・モデル別。同上 |
| `settings.snippet.json` | — | 上の2本を自動で走らせる設定。**既存の設定へ追記する**（後述） |

**オーケストレーターのエージェント定義ファイルは無い。** オーケストレーターは**メインセッション自身**である。サブエージェントは `AskUserQuestion` を使えず、PO へ問い合わせできないため、サブエージェントであり得ない。

## 導入

```sh
# 開発対象リポジトリのルートで
mkdir -p .claude/agents .claude/rules .claude/skills
mkdir -p docs
cp    /path/to/agents-org/org/glossary.md        docs/
cp -r /path/to/agents-org/org/agents/*        .claude/agents/
cp -r /path/to/agents-org/org/rules/*         .claude/rules/
cp -r /path/to/agents-org/org/skills/*        .claude/skills/
mkdir -p .claude/scripts .claude/templates
cp    /path/to/agents-org/org/scripts/org-check.py   .claude/scripts/
cp    /path/to/agents-org/org/scripts/org-tokens.py  .claude/scripts/
cp -r /path/to/agents-org/org/templates/*        .claude/templates/
```

**台帳そのもの（索引・詳細・キュー）は、この時点では作らない。** ゴールを受けたオーケストレーターが最初のタスクを登録する直前に、雛形から作る（手順は `skills/org-orchestrate/`）。空の台帳を先に置くと、組織が動いていないのか、動いて0件なのかが区別できない。

`org/CLAUDE.md` は、開発対象リポジトリのルート `CLAUDE.md` へ**追記**する。既に `CLAUDE.md` があれば置き換えず、組織の節として足す。開発対象そのものの規約（技術スタック、ビルド手順、コーディング規約）は、その下へ書く。

### 既存拡張との共存

Everything Claude Code / Superpowers 等が導入済みでも共存する前提である。

- すべて `org-` プレフィックスを持つ。**既存の commands / skills / hooks を上書き・削除しない**
- hook を足す場合は、既存エントリを**置換せず追記**する
- 名前が衝突したら**組織側が譲る**（`org-` の後ろを変える）

### 検査と集計を自動で走らせる

`settings.snippet.json` の中身を、開発対象リポジトリの `.claude/settings.json` へ**追記**する。

**ファイルごと上書きしない。** 既存の設定（他の拡張が入れた hook を含む）を消してしまう。`hooks` の項目が既にあるなら、その中の配列へ足す形にする。

入っているのは3件で、走る場面（Claude Code が用意している呼び出し口の名前）が2種類ある。

| 呼び出し口 | 走るもの | 何のため |
| --- | --- | --- |
| `SessionStart`（セッションが始まったとき） | `org-check.py --hook` | 台帳の停滞検知と整合性検査 |
| `SessionStart` | `org-tokens.py --update --hook` | 前のセッションまでのトークン消費を台帳へ取り込む |
| `SubagentStop`（担当エージェントが1体終わったとき） | `org-tokens.py --update --hook` | その担当が使った分を、終わった時点で記録する |

トークン集計を2箇所へ入れてあるのは取りこぼしを防ぐためである。担当エージェントの終了時に記録するのが本筋だが、セッションが異常終了した場合や、担当エージェントを使わなかった場合の分が落ちる。セッション開始時にもう一度、**過去の分まで含めて**読み直すことで拾い直す。**この集計は何度走らせても結果が変わらない**ように作ってあるので、二重に走っても数字は狂わない。

**`CLAUDE.md` へ書くだけでは駄目な理由**: `CLAUDE.md` は読まれる文脈であって、実行される設定ではない。「セッション開始時に停滞を確認せよ」と書いても、実行されない回が出る。必ず走らせたいものは hook にする。

### Python の呼び出し名に注意する

**`python3` というコマンド名は、どの環境にもあるわけではない。** Windows で python.org の配布物を入れた場合、存在するのは `python` だけで `python3` は無い（Microsoft Store 版と多くの Linux / macOS では逆に `python3` がある）。

このため、セッション開始時に検査を走らせる設定（`settings.snippet.json`）は `python3` を試し、失敗したら `python` を試す形にしてある。**手で実行するときも、片方が「コマンドが見つからない」と言ったらもう片方に読み替える。** 以下の例はすべて `python3` と書いてあるが、この読み替えが要る環境がある。

### 停滞検知と整合性検査の使い方

```sh
python3 .claude/scripts/org-check.py              # 停滞検知と整合性検査
python3 .claude/scripts/org-check.py --summary    # ゴール健全性の指標を集計
python3 .claude/scripts/org-check.py --statusline # 1行にまとめる（ステータス行向け）
python3 .claude/scripts/org-check.py --days 3     # 停滞と判定する日数を変える（既定 2）
```

検出するもの:

| 種別 | 中身 |
| --- | --- |
| 停滞候補 | 更新が止まっているタスク（実時間基準）、手戻りが続いているタスク（サイクル基準） |
| PO へのリマインド候補 | 回答が来ていない PO確認待ち。**組織側の停滞とは分けて出す** |
| 整合性の警告 | 索引と詳細ファイルの食い違い、書式違反、依存先の不在、担当の未割当、**指示欄（完了条件・判断してよい範囲・変更範囲・禁止事項）が未記入のまま担当が付いていること**、**完了なのに証拠が空**、PO確認待ちキューの書式違反 |

**雛形の案内文（`<!-- ... -->`）が残っている節は、未記入として扱う。** 「書いたつもりで空」を検出するための仕様である。

終了コードは、検出なしで `0`、停滞や警告ありで `1`、台帳が読めないときに `2`。

**このスクリプトは判定するだけで、対処はしない。** 何をするかはオーケストレーターが決める。スクリプトが勝手に担当を変えると、なぜそうなったかが記録に残らないため。

### トークン消費の集計の使い方

Claude Code の定額プランには、1日・1週間で使える量の上限がある。その枠内でこなせるタスクを増やすには、まず**どのタスクの、どの担当が、どのモデルで、どれだけ食っているか**が見えていなければならない。それを出すのが `org-tokens.py` である。

```sh
python3 .claude/scripts/org-tokens.py --update    # 会話記録を読んで台帳を更新する
python3 .claude/scripts/org-tokens.py             # タスク別に集計して表示（既定）
python3 .claude/scripts/org-tokens.py --by agent  # 担当エージェント別
python3 .claude/scripts/org-tokens.py --by model  # モデル別（Haiku / Sonnet / Opus の別）
python3 .claude/scripts/org-tokens.py --task T-007  # 1タスクの内訳（担当別・モデル別）
python3 .claude/scripts/org-tokens.py --statusline  # 1行にまとめる
```

出力はこの形になる。

```
■ トークン消費（タスクID別）
  タスクID  合計         出力     キャッシュ書込  キャッシュ読出  担当数  返答回数
  ---------------------------------------------------------------------------
  T-003     106,413,926  436,106       3,482,530     102,494,554       4       369
  T-001       5,430,995   28,992         446,829       4,955,073       2        52
```

**データはどこから来るか** — 新しく記録を取る仕組みは作っていない。Claude Code が既に書き出している会話記録（`~/.claude/projects/` の下にある、1行1イベントの JSONL 形式のファイル）を読むだけである。モデルの返答1回ごとに、使ったトークン数が4種類（新規入力・出力・キャッシュ書込・キャッシュ読出）に分かれて記録されており、担当エージェントの分は別ファイルに、その種別（`org-implementation` などの名前）と一緒に残っている。

**タスクへの紐付け** — 「このトークンは T-007 のもの」という情報だけは記録に無いので、指示文から拾う。

| 対象 | やり方 | 精度 |
| --- | --- | --- |
| 担当エージェント | 記録の先頭にある指示文から `T-007` の形を探す。担当エージェントへの指示は自己完結でなければならない（会話履歴を引き継げないため）ので、タスクIDはそこにほぼ必ず書かれている | **正確** |
| オーケストレーター（メインセッション） | 時系列に読み進め、タスクIDが現れたら、それ以降の返答をそのタスクのものとみなす。次のIDが現れたら切り替える | **近似**。最初のIDが現れる前は `タスク外` に入る |

近似が混じるのはメインセッション側だけである。「どのタスクが重かったか」の比較には十分効くが、**1トークン単位の正確な帰属ではない**ことを前提に読むこと。

**記録の置き場** — `docs/token-usage-{project-name}.csv`。タスク台帳（`docs/task-list-{project-name}.csv`）とは**別ファイルにしてある**。タスク台帳は「何をやるか」の記録でオーケストレーターだけが書くと決まっているのに対し、こちらは「いくら食ったか」の記録でスクリプトが機械的に上書きする。混ぜると、集計が走るたびにタスク台帳の差分が汚れ、誰が何を書き換えたのかが追えなくなる。

**この台帳は手で書かない。** 消えても `--update` で会話記録から作り直せる。ただし Claude Code が古い会話記録を消した後は作り直せないので、**ファイルごと消さない**こと（記録が残っていないセッションの行は、集計時もそのまま保存される）。

**金額は出さない。** 定額プランでは請求額と対応しないため、今は量だけを見る。金額への換算は今後の課題（`docs/decisions/16-token-visibility.md` の未確定事項）。

## まだ入っていないもの

- 人間用ビュー（README / handbook）の生成スクリプト。`docs/decisions/06-documentation-agent.md` の確定事項 A

## 配布方式について

現状は手動コピーである。Claude Code のプラグイン機構でまとめれば「ファイル追加だけで導入できる」と「既存拡張と共存する」の両方をより強く満たせる可能性があるが、MVP では手動コピーで足りると判断した。**PO の判断を仰ぐべき論点として残してある**（`docs/decisions/13-execution-form.md` の未確定事項）。
