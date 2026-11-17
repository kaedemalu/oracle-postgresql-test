SET search_path = app, public;

CREATE TABLE customers (
  id          numeric(10)   CONSTRAINT customers_pk PRIMARY KEY,
  name        varchar(50)   NOT NULL,
  rank        char(1),
  created_at  timestamp(0)
);

CREATE TABLE orders (
  id          numeric(10)   CONSTRAINT orders_pk PRIMARY KEY,
  customer_id numeric(10)   NOT NULL CONSTRAINT orders_customer_fk REFERENCES customers(id),
  amount      numeric(12,2) NOT NULL CONSTRAINT orders_amount_ck CHECK (amount >= 0),
  tax         numeric(12,2),
  status      varchar(10)   DEFAULT 'NEW' NOT NULL,
  note        varchar(100),
  order_date  timestamp(0),
  updated_at  timestamp(0)
);

CREATE TABLE audit_log (
  id          numeric(10)   CONSTRAINT audit_log_pk PRIMARY KEY,
  order_id    numeric(10),
  action      varchar(10),
  old_status  varchar(10),
  new_status  varchar(10),
  logged_at   timestamp(0)
);

CREATE SEQUENCE order_seq START WITH 1 CACHE 1;
CREATE SEQUENCE audit_seq START WITH 1 CACHE 1;

CREATE VIEW v_customer_totals AS
SELECT c.id, c.name, count(o.id) AS order_count, coalesce(sum(o.amount), 0) AS total_amount
  FROM customers c
  LEFT JOIN orders o ON o.customer_id = c.id
 GROUP BY c.id, c.name;
