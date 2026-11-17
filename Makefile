# 全てコンテナ内で実行する。ARGS で pytest に追加引数を渡せる(例: make verify ARGS="-k CALC_TAX")
COMPOSE := docker compose
RUN     := $(COMPOSE) run --rm runner

.PHONY: up down build deploy unit record record-update check-oracle verify diff lint-pg ledger-check ci shell logs

up:            ## DB コンテナ起動(healthy まで待つ)
	$(COMPOSE) up -d --wait oracle postgres

down:          ## 停止・削除(データも破棄)
	$(COMPOSE) down -v

build:
	$(COMPOSE) build

deploy: up     ## スキーマ・パッケージ・関数を両 DB に適用
	$(RUN) python -m tests.tools.deploy all

unit:          ## 基盤自体の単体テスト(DB 不要)
	$(COMPOSE) run --rm --no-deps runner pytest tests/unit -q

record:        ## Oracle で実行しゴールデンを生成
	$(RUN) pytest tests/test_runner.py --mode=record $(ARGS)

record-update: ## 既存ゴールデンを上書き(差分はレビュー対象)
	$(RUN) pytest tests/test_runner.py --mode=record --update-golden $(ARGS)

check-oracle:  ## Oracle 再実行でゴールデンの再現性確認
	$(RUN) pytest tests/test_runner.py --mode=check-oracle $(ARGS)

verify:        ## PostgreSQL で実行しゴールデンと照合
	$(RUN) pytest tests/test_runner.py --mode=verify --target=postgres $(ARGS)

diff:          ## 両 DB で実行し直接比較
	$(RUN) pytest tests/test_runner.py --mode=diff $(ARGS)

lint-pg:       ## plpgsql_check による静的解析
	$(RUN) python -m tests.tools.lint_pg

ledger-check:  ## 差異台帳のリリース判定
	$(COMPOSE) run --rm --no-deps runner python -m tests.tools.ledger check

ci: deploy unit check-oracle verify ledger-check  ## 15.2 の CI パイプライン

shell:
	$(RUN) bash

logs:
	$(COMPOSE) logs -f oracle postgres
