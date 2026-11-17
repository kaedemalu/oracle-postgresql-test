-- PKG_ORDER の変換版(スキーマ方式: パッケージ名 → スキーマ名)
-- ユーザー定義例外の規約: RAISE_APPLICATION_ERROR(-20nnn) → ERRCODE 'U0nnn'
CREATE SCHEMA pkg_order;

CREATE FUNCTION pkg_order.calc_tax(p_amount numeric) RETURNS numeric
LANGUAGE plpgsql AS $$
BEGIN
  IF p_amount IS NULL THEN
    RETURN NULL;
  END IF;
  IF p_amount < 0 THEN
    RAISE EXCEPTION 'amount must be >= 0' USING ERRCODE = 'U0001';
  END IF;
  RETURN trunc(p_amount * 0.1);
END;
$$;

CREATE FUNCTION pkg_order.format_name(p_last varchar, p_first varchar) RETURNS varchar
LANGUAGE plpgsql AS $$
BEGIN
  RETURN trim(coalesce(p_last, '') || ' ' || coalesce(p_first, ''));
END;
$$;

CREATE FUNCTION pkg_order.month_end_after(p_date timestamp, p_months numeric) RETURNS timestamp(0)
LANGUAGE plpgsql AS $$
BEGIN
  IF p_date IS NULL OR p_months IS NULL THEN
    RETURN NULL;
  END IF;
  -- LAST_DAY(ADD_MONTHS(d, n)): 月末日 + 元の時刻部分
  RETURN date_trunc('month', p_date + make_interval(months => trunc(p_months)::int))
         + interval '1 month' - interval '1 day'
         + (p_date - date_trunc('day', p_date));
END;
$$;

CREATE FUNCTION pkg_order.find_customer_id(p_name_prefix varchar) RETURNS numeric
LANGUAGE plpgsql AS $$
DECLARE
  v_id app.customers.id%TYPE;
BEGIN
  -- Oracle の NULL || '%' = '%' を再現するため coalesce する
  SELECT id INTO STRICT v_id FROM app.customers WHERE name LIKE coalesce(p_name_prefix, '') || '%';
  RETURN v_id;
END;
$$;

CREATE FUNCTION pkg_order.top_orders(p_n numeric) RETURNS refcursor
LANGUAGE plpgsql AS $$
DECLARE
  v_cur refcursor;
BEGIN
  OPEN v_cur FOR
    SELECT id, amount FROM app.orders ORDER BY amount DESC, id LIMIT greatest(p_n, 0);
  RETURN v_cur;
END;
$$;

CREATE PROCEDURE pkg_order.create_order(p_customer_id numeric, p_amount numeric, p_note varchar,
                                        OUT p_order_id numeric, OUT p_tax numeric)
LANGUAGE plpgsql AS $$
BEGIN
  p_tax := pkg_order.calc_tax(p_amount);
  p_order_id := nextval('app.order_seq');
  INSERT INTO app.orders (id, customer_id, amount, tax, status, note, order_date, updated_at)
  VALUES (p_order_id, p_customer_id, p_amount, p_tax, 'NEW', p_note, app_clock.now(), localtimestamp(0));
  RAISE NOTICE 'created order %', p_order_id;
END;
$$;

CREATE PROCEDURE pkg_order.change_status(p_order_id numeric, p_status varchar)
LANGUAGE plpgsql AS $$
BEGIN
  UPDATE app.orders SET status = p_status, updated_at = localtimestamp(0) WHERE id = p_order_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'order not found: %', p_order_id USING ERRCODE = 'U0002';
  END IF;
END;
$$;

CREATE PROCEDURE pkg_order.get_orders(p_customer_id numeric, OUT p_cur refcursor)
LANGUAGE plpgsql AS $$
BEGIN
  OPEN p_cur FOR
    SELECT id, amount, status FROM app.orders WHERE customer_id = p_customer_id ORDER BY id;
END;
$$;
