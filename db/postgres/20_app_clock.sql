-- 時刻取得ラッパ(仕様 5.6 (b) 方式)。セッション変数 app.fixed_clock に固定値を注入する
CREATE SCHEMA app_clock;

CREATE FUNCTION app_clock.set_fixed(p_ts timestamp) RETURNS void
LANGUAGE sql AS $$
  SELECT set_config('app.fixed_clock', coalesce(p_ts::text, ''), false);
$$;

CREATE FUNCTION app_clock.clear() RETURNS void
LANGUAGE sql AS $$
  SELECT set_config('app.fixed_clock', '', false);
$$;

CREATE FUNCTION app_clock.now() RETURNS timestamp(0)
LANGUAGE plpgsql STABLE AS $$
DECLARE
  v text := current_setting('app.fixed_clock', true);
BEGIN
  IF v IS NULL OR v = '' THEN
    RETURN localtimestamp(0);
  END IF;
  RETURN v::timestamp(0);
END;
$$;
