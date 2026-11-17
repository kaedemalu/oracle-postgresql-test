CREATE EXTENSION IF NOT EXISTS plpgsql_check;
DO $$
BEGIN
  EXECUTE format('ALTER DATABASE %I SET timezone TO ''UTC''', current_database());
  EXECUTE format('ALTER DATABASE %I SET datestyle TO ''ISO, YMD''', current_database());
END $$;
