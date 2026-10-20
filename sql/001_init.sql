CREATE TABLE IF NOT EXISTS schema_migrations(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS machines(id bigserial PRIMARY KEY,name text NOT NULL UNIQUE,location text NOT NULL DEFAULT '',active integer NOT NULL DEFAULT 1 CHECK(active IN(0,1)));
CREATE TABLE IF NOT EXISTS workers(id bigserial PRIMARY KEY,name text NOT NULL,personnel_no text UNIQUE,machine_id bigint REFERENCES machines(id),active integer NOT NULL DEFAULT 1 CHECK(active IN(0,1)));
CREATE TABLE IF NOT EXISTS role_definitions(code text PRIMARY KEY,name text NOT NULL UNIQUE,permissions_json jsonb NOT NULL DEFAULT '[]',is_system integer NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS users(id bigserial PRIMARY KEY,username text NOT NULL UNIQUE,password_hash text NOT NULL,role text NOT NULL REFERENCES role_definitions(code),worker_id bigint UNIQUE REFERENCES workers(id),active integer NOT NULL DEFAULT 1 CHECK(active IN(0,1)),created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS worker_machines(worker_id bigint REFERENCES workers(id),machine_id bigint REFERENCES machines(id),PRIMARY KEY(worker_id,machine_id));
CREATE TABLE IF NOT EXISTS products(id bigserial PRIMARY KEY,code text NOT NULL UNIQUE,item_class text NOT NULL CHECK(item_class IN('consumable','fixture')),category text NOT NULL,name text NOT NULL,model text NOT NULL DEFAULT '',manufacturer text NOT NULL DEFAULT '',parameters text NOT NULL DEFAULT '',unit text NOT NULL DEFAULT 'шт',min_qty bigint NOT NULL DEFAULT 0 CHECK(min_qty>=0),inventory_no text,params_json jsonb NOT NULL DEFAULT '{}',price_per_unit numeric(18,2) NOT NULL DEFAULT 0 CHECK(price_per_unit>=0),location text NOT NULL DEFAULT '',photo text,active integer NOT NULL DEFAULT 1 CHECK(active IN(0,1)),created_at timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX IF NOT EXISTS ux_products_inventory_no ON products(inventory_no) WHERE inventory_no IS NOT NULL AND inventory_no<>'';
CREATE TABLE IF NOT EXISTS movements(id bigserial PRIMARY KEY,batch_id text NOT NULL,occurred_at timestamptz NOT NULL DEFAULT now(),product_id bigint NOT NULL REFERENCES products(id),kind text NOT NULL CHECK(kind IN('receipt','issue','return','writeoff_stock','writeoff_issued','regrind_send','regrind_receive')),qty bigint NOT NULL CHECK(qty>0),state_from text,state_to text,worker_id bigint REFERENCES workers(id),machine_id bigint REFERENCES machines(id),actor_user_id bigint REFERENCES users(id),note text NOT NULL DEFAULT '',voided_at timestamptz,voided_by_user_id bigint REFERENCES users(id),void_reason text NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_movements_product ON movements(product_id);
CREATE INDEX IF NOT EXISTS ix_movements_date ON movements(occurred_at);
CREATE INDEX IF NOT EXISTS ix_movements_worker ON movements(worker_id,machine_id);
CREATE TABLE IF NOT EXISTS movement_changes(id bigserial PRIMARY KEY,movement_id bigint NOT NULL REFERENCES movements(id),changed_at timestamptz NOT NULL DEFAULT now(),actor_user_id bigint NOT NULL REFERENCES users(id),action text NOT NULL,reason text NOT NULL,before_json jsonb NOT NULL,after_json jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token text PRIMARY KEY,user_id bigint NOT NULL REFERENCES users(id),csrf text NOT NULL,expires_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS submitted_forms(user_id bigint REFERENCES users(id),request_id text NOT NULL,fingerprint text NOT NULL,response jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(user_id,request_id));
CREATE TABLE IF NOT EXISTS purchase_requests(id bigserial PRIMARY KEY,request_no text UNIQUE,created_at timestamptz NOT NULL DEFAULT now(),created_by bigint NOT NULL REFERENCES users(id),status text NOT NULL DEFAULT 'Создана',closed_at timestamptz,closed_by bigint REFERENCES users(id),archived_at timestamptz,archived_by bigint REFERENCES users(id));
CREATE TABLE IF NOT EXISTS purchase_request_lines(id bigserial PRIMARY KEY,request_id bigint NOT NULL REFERENCES purchase_requests(id),product_id bigint NOT NULL REFERENCES products(id),code text NOT NULL,name text NOT NULL,category text NOT NULL,qty bigint NOT NULL CHECK(qty>0),unit text NOT NULL,unit_price numeric(18,2) NOT NULL DEFAULT 0,stock_at_request bigint NOT NULL,min_at_request bigint NOT NULL);
CREATE OR REPLACE VIEW inventory AS
 SELECT p.id product_id,s.state,COALESCE(SUM(CASE
 WHEN m.kind IN('receipt','return') AND m.state_to=s.state THEN m.qty
 WHEN m.kind IN('regrind_send','regrind_receive') AND m.state_to=s.state THEN m.qty
 WHEN m.kind IN('issue','writeoff_stock') AND m.state_from=s.state THEN -m.qty
 WHEN m.kind IN('regrind_send','regrind_receive') AND m.state_from=s.state THEN -m.qty ELSE 0 END),0)::bigint qty
 FROM products p CROSS JOIN (VALUES('new'),('used'),('sharpened'),('regrind_queue'),('regrind')) s(state)
 LEFT JOIN movements m ON m.product_id=p.id AND m.voided_at IS NULL GROUP BY p.id,s.state;
CREATE OR REPLACE VIEW holdings AS
 SELECT product_id,worker_id,machine_id,s.state,SUM(CASE WHEN kind='issue' AND state_to=s.state THEN qty WHEN kind IN('return','writeoff_issued') AND state_from=s.state THEN -qty ELSE 0 END)::bigint qty
 FROM movements CROSS JOIN (VALUES('new'),('used'),('sharpened')) s(state)
 WHERE voided_at IS NULL AND worker_id IS NOT NULL GROUP BY product_id,worker_id,machine_id,s.state
 HAVING SUM(CASE WHEN kind='issue' AND state_to=s.state THEN qty WHEN kind IN('return','writeoff_issued') AND state_from=s.state THEN -qty ELSE 0 END)<>0;
INSERT INTO schema_migrations(version) VALUES(1) ON CONFLICT DO NOTHING;
