FROM postgres:16-bookworm
# plpgsql_check(静的解析)を同梱。PGDG apt リポジトリは公式イメージで設定済み
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-16-plpgsql-check \
 && rm -rf /var/lib/apt/lists/*
COPY init.sql /docker-entrypoint-initdb.d/00_init.sql
