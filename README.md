# PL/SQL ⇔ PL/pgSQL 二刀流検証テスト基盤

[SPECIFICATION.md](SPECIFICATION.md) の実装。Oracle の実行結果をゴールデンマスタとして記録し、
PostgreSQL への変換後の同一ケースと照合してデグレを検出する。DB もテストランナーも全てコンテナで動く。

## クイックスタート

```sh
cp .env.example .env      # 任意(既定値のままでも動く)
make deploy               # Oracle / PostgreSQL 起動 + db/ 配下のスキーマ・パッケージを適用
make unit                 # 基盤自体の単体テスト(DB 不要)
make record               # Oracle で実行 → tests/golden/*.json を生成
make check-oracle         # Oracle 再実行でゴールデンの再現性確認
make verify               # PostgreSQL で実行 → ゴールデンと照合
```

その他: `make diff`(両 DB 直接比較)/ `make record-update`(ゴールデン上書き)/ `make lint-pg`(plpgsql_check)/
`make ledger-check`(台帳のリリース判定)/ `make ci`(15.2 のパイプライン一式)/ `make down`(破棄)。

pytest の引数は `ARGS` で渡す:`make verify ARGS="-k CALC_TAX"`、`ARGS="-m dialect"`(タグ)、
`ARGS="-m priority_high"`、`ARGS="--sql-log"`(実行 SQL・バインド値を `sql.log` に出力)。

## 構成

| コンテナ | イメージ | 備考 |
| --- | --- | --- |
| `oracle` | `gvenzl/oracle-free:23-slim-faststart` | AL32UTF8 / UTC。アプリユーザ `app`(FREEPDB1) |
| `postgres` | `postgres:16` + `plpgsql_check` | UTF8 / `C.UTF-8` / UTC |
| `runner` | `python:3.12-slim` | pytest + python-oracledb(Thin)+ psycopg 3。リポジトリを `/work` にマウント |

```
config/        settings / error_map / normalize / tolerances
db/oracle/     Oracle 側の原本(サンプル: PKG_ORDER, APP_CLOCK, TRG_ORDERS_AUDIT, V_CUSTOMER_TOTALS)
db/postgres/   PostgreSQL 側の変換版
tests/cases/   DB 非依存のケース定義(YAML)
tests/fixtures/ テストデータ(CSV / YAML)
tests/golden/  Oracle で記録した期待値(Git 管理・レビュー対象)
tests/adapters/ OracleAdapter / PostgresAdapter
tests/core/    loader / normalize / compare / errors / runner / report / models
tests/tools/   deploy / ledger / lint_pg
reports/       実行ごとに <時刻>-<mode>/ に summary.md, report.html, results.json, junit.xml
ledger/        差異台帳 diff_ledger.csv
```

## 1 ケースの実行手順

ケースごとに新しいセッションを張り、次の順で実行する。

1. `reset`: `settings.reset.tables` を子→親の順に DELETE、シーケンスを既知値へ RESTART
2. 時刻注入(`clock`)— 事前データ投入で発火するトリガにも効くよう、投入より前に行う
3. fixtures → インライン行 → 補助 SQL の投入、COMMIT
4. 呼び出し(PostgreSQL はセーブポイント内。例外時はセーブポイントまで戻す)
5. `transaction.commit_after_call` が true なら COMMIT
6. 観測(`observe_from: other_session` なら別接続でコミット済みのみ)→ 正規化 → 比較

## 判定

`PASS` / `PASS_WITH_WARNING`(空文字 '' を NULL と同一視した等)/ `TOLERATED` / `FAIL` / `ERROR` / `SKIP`。
pytest 上は FAIL・ERROR のみ失敗扱い。`ERROR` になるのは接続断、`ERR_UNMAPPED`、ゴールデン未記録・陳腐化
(ケース/フィクスチャのハッシュ不一致)など。

## ケースの追加手順

1. `tests/cases/<スイート>/<対象>.yaml` にケースを書く(スキーマは仕様 5.2、`defaults` はスイート内で deep merge)
2. 必要ならデータを `tests/fixtures/` に追加([fixtures/README](tests/fixtures/README.md))
3. `make record ARGS="-k <ケースID>"` → 生成された `tests/golden/<id>.json` の中身を確認してコミット
4. `make verify ARGS="-k <ケースID>"`
5. FAIL が出たら `python -m tests.tools.ledger sync reports/<run>/results.json` で台帳に登録し、分類する。
   仕様差として認める場合は `config/tolerances.yaml` に許容差を追加し、台帳の `resolution` に許容差 ID を書く

定義が不正(未知のキー、`function` に `returns` 無し、skip に理由無し、ID 重複など)な場合は、収集時点でエラーになる。

## ケース YAML の書き方

1 ファイルに 1 スイート。`defaults` は各ケースに deep merge される(ケース側の値が優先)。
実例は [tests/cases/](tests/cases/) を参照。

```yaml
suite: PKG_ORDER                       # スイート名(レポートの集計単位)
defaults:
  clock: "2026-01-15T10:00:00"         # 時刻注入。APP_CLOCK.NOW / app_clock.now() がこの値を返す
  observe_scope: [ORDERS, AUDIT_LOG]   # observe.snapshot.tables が空のときに使うテーブル
  target: {kind: procedure, name: PKG_ORDER.CREATE_ORDER}
  setup:
    fixtures: [base_master]            # tests/fixtures/ のデータセット名

cases:
  - id: PKG_ORDER.CREATE_ORDER.001     # 全体で一意。<スイート>.<対象>.<連番3桁>
    title: 正常登録
    tags: [regression]                 # pytest -m <tag> で絞り込める
    priority: high                     # high / medium / low(-m priority_high)
    call:
      args:
        - {name: P_CUSTOMER_ID, type: INTEGER, value: 1}
        - {name: P_AMOUNT, type: NUMBER, value: "1000.50"}   # 精度が必要な数値は文字列で
        - {name: P_NOTE, value: ""}                           # type 省略時は VARCHAR
        - {name: P_ORDER_ID, type: INTEGER, mode: OUT}
    observe:
      messages: true                   # DBMS_OUTPUT / RAISE NOTICE を比較する
      snapshot:
        ignore_columns: {ORDERS: [UPDATED_AT]}
        mask: {ORDERS.ORDER_DATE: NOW}  # SEQ / NOW / GUID
```

### 主な項目

| 項目 | 既定値 | 内容 |
| --- | --- | --- |
| `target.kind` | (必須) | `function` / `procedure` / `trigger` / `sql` |
| `target.name` | — | 論理名。DB ごとの実名は `settings.yaml` で解決される |
| `target.invoke_style` | `block` | `function` の呼び方。`select` にすると `SELECT f(...) FROM DUAL` で呼ぶ |
| `call.args[]` | `[]` | `name` / `type`(論理型)/ `value` / `mode`(`IN` / `OUT` / `INOUT`)/ `pg_type`(PG 側のキャスト型を明示する場合) |
| `call.returns.type` | — | `function` では必須。`REFCURSOR` にすると、返ってきた行を結果セット `RETURN` として比較する |
| `fire` | — | `trigger` では必須。`{table, op: insert/update/delete, data, set, where}` の形で書く |
| `query` | — | `sql` では必須。文字列(両 DB 共通)か `{oracle: ..., postgres: ...}` で書く。結果は結果セット `QUERY` として比較する |
| `setup.rows` | `[]` | インラインのデータ投入。`[{table, data: [{列: 値}]}]` の形で書く |
| `setup.sql` | `{}` | DB 固有の補助 SQL `{oracle: [...], postgres: [...]}`。他の手段が無いときだけ使う |
| `observe.return` / `out_args` / `result_sets` | `true` | それぞれを比較対象にするか |
| `observe.messages` | `false` | `DBMS_OUTPUT` / `RAISE NOTICE` を比較するか |
| `observe.ordered` | `false` | 結果セットの行の順序も比較するか(`ORDER BY` が仕様の一部である場合に `true`) |
| `observe.snapshot` | 無し | 書いたときだけ事後状態を観測する。`tables` / `mode`(`scoped` / `schema_wide`)/ `ignore_columns` / `mask` を指定できる |
| `error.compare` | `name` | `name`(論理エラー名だけ比較)/ `name_and_message`(メッセージ本文も比較)/ `any`(例外が起きたかどうかだけ比較) |
| `transaction.commit_after_call` | `false` | 呼び出しの後に COMMIT するか |
| `transaction.observe_from` | `same_session` | `other_session` にすると別の接続から観測する(コミット済みのデータだけが見える) |
| `strict_empty` | `false` | `true` にすると `''` と NULL を区別して比較する |
| `skip` | — | `{oracle: bool, postgres: bool, reason: 必須}` |

**論理型:** `NUMBER` / `INTEGER` / `FLOAT` / `VARCHAR` / `CHAR` / `DATE` / `TIMESTAMP` / `TIMESTAMPTZ` / `BOOLEAN` / `CLOB` / `BLOB` / `REFCURSOR`(OUT 専用)

**値の書き方:**
- 日時は ISO8601(`2026-01-15T10:00:00`)で書く
- BLOB は Base64 で書く
- `null` は NULL として渡す
- `""` は、Oracle には NULL、PostgreSQL には `''` として渡す

ケースの定義を変更するとハッシュが変わり、既存の期待値は「陳腐化」として `ERROR` になる。その場合は `make record-update` で期待値を記録し直す。

## 設定ファイル(config/)

| ファイル | 内容 | 主なキー |
| --- | --- | --- |
| `settings.yaml` | 接続先と実行時の動作 | `databases.<db>`(接続先・セッション固定パラメータ。`${ENV:-既定値}` で環境変数を展開する)、`allowed_hosts`(ここに無いホストには接続しない)、`naming` / `name_mapping`(名前解決)、`clock`(時刻注入に使う SQL)、`reset`(各ケースの前に削除するテーブルを子→親の順で並べたものと、シーケンスの初期値)、`golden_source`(ゴールデンの記録元として想定する `version_prefix` と `nls`。指定すると `verify` / `check-oracle` で一致を確認し、不一致は `ERROR`)、`deploy`、`paths` |
| `error_map.yaml` | 例外の対応表 | `errors[]`: `{name, oracle: [ORA-xxxxx または {code, message_regex}], postgres: [SQLSTATE または {code, message_regex}]}`(多対一も可。上から順に評価し、最初に一致したものを採用。ORA-06502 は本文で振り分けている)。`user_errors`: ユーザー定義例外の番号の対応規約。表に無いエラーは `ERR_UNMAPPED` となり `ERROR` になる |
| `normalize.yaml` | 正規化ルール | `string`(NFC 正規化 / `char_map` による文字の読み替え(波ダッシュ等)/ 改行の統一 / CHAR 型の末尾空白除去)、`empty_string_as_null`、`datetime.precision`(`second` / `microsecond`)、`boolean`(真偽値とみなす値のリスト)、`float.digits`、`columns[]`(列単位の上書き。`{table, column}` は glob で指定し、`mask` または `type` を設定する) |
| `tolerances.yaml` | 承認済みの仕様差 | `tolerances[]`: `{id, scope: {case, path}, rule, reason, approved_by, approved_at, expires}`。`rule` は `ignore` / `{numeric_abs: n}` / `{numeric_rel: n}`。必須項目が欠けているとテストを開始しない。期限切れのものは適用しない |

`scope.path` には、レポートに表示される差分パスを glob で書く(例: `observed.snapshot.ORDERS*.AMOUNT`、`observed.error.message`)。
なお glob では `[...]` が文字クラスとして扱われるため、行キーの部分は `*` で表す。

接続先の認証情報は `.env`(Git 管理外)で設定する。`compose.yaml` がそれを runner コンテナの環境変数として渡す。

## 仕様の未決事項に対して置いた仮決め

| # | 項目 | 実装での扱い | 変更箇所 |
| --- | --- | --- | --- |
| 1 | DB バージョン | Oracle 23ai Free / PostgreSQL 16 | `compose.yaml` |
| 3 | パッケージ変換方式 | スキーマ方式(`PKG_ORDER.CALC_TAX` → `pkg_order.calc_tax`) | `settings.yaml` の `naming` / `name_mapping` |
| 6 | 時刻固定 | ラッパ関数差し替え(Oracle: `APP_CLOCK` パッケージ変数、PG: `app.fixed_clock` セッション変数) | `settings.yaml` の `clock` |
| 7 | ユーザー定義例外 | `RAISE_APPLICATION_ERROR(-20nnn)` ⇔ `ERRCODE 'U0nnn'`。`P0001` + メッセージ先頭 `[20nnn]` も可 | `error_map.yaml` の `user_errors` |
| — | BOOLEAN | Oracle には 1/0 で渡す。PL/SQL の BOOLEAN 引数・戻り値はローカル変数経由の DECLARE ブロックで 1/0 に変換する(`invoke_style: select` は不可) | `adapters/oracle.py` の `bool_value` / `build_plsql_call` |
| — | リセット | TRUNCATE ではなく DELETE(Oracle は FK 参照される親を TRUNCATE できないため) | `settings.yaml` の `reset` |

## 未実装・制約

- 配列 / レコード / コレクション型の引数(未決事項 #8)
- `pytest-xdist` による並列実行(ワーカー別スキーマの割当て)。現状は単一スキーマで直列実行
- utPLSQL によるカバレッジ計測との連携
- PostgreSQL のプロシージャ内 COMMIT(`CALL` をトランザクション外で呼ぶ必要がある)
- PG 関数の引数が `date` 型などキャスト既定と異なる場合は、ケースの引数に `pg_type` を明示する
- 仕様 4 章の `-update-golden` は `--update-golden` として実装
- 開発環境(2 vCPU / 4GB の VM)では、初回起動時の CPU 逼迫による時刻ドリフトで Oracle が停止することがあった。
  その場合は `docker compose rm -sf oracle && make up` で作り直す
