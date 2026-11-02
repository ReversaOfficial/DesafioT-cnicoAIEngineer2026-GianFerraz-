-- Esquema analítico derivado dos CSVs da operação.
--
-- Decisões de modelagem:
--   * Colunas `*_norm` guardam texto sem acento e em minúsculas. O SQLite não
--     tem função de remoção de acentos, então a normalização é feita no ETL
--     (Python) e persistida — assim a busca "violao yamaha" encontra
--     "Violão Yamaha" sem custo em tempo de consulta.
--   * `availability` é um campo derivado que combina `status` + `stock_quantity`
--     em um único vocabulário fechado. Evita que cada ferramenta reimplemente
--     (e divirja em) essa regra de negócio.
--   * `brand` é extraída do nome do produto no ETL para permitir filtro por
--     marca, que é um pedido recorrente no atendimento.

PRAGMA foreign_keys = ON;

DROP VIEW  IF EXISTS v_product_pricing;
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS promotions;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS categories;
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS meta;

CREATE TABLE categories (
    category_id   INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    name_norm     TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE products (
    product_id       INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    name_norm        TEXT NOT NULL,
    brand            TEXT NOT NULL DEFAULT '',
    brand_norm       TEXT NOT NULL DEFAULT '',
    category_id      INTEGER NOT NULL REFERENCES categories(category_id),
    price_brl        REAL NOT NULL CHECK (price_brl >= 0),
    stock_quantity   INTEGER NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0),
    status           TEXT NOT NULL,            -- active | discontinued | coming_soon
    availability     TEXT NOT NULL,            -- in_stock | out_of_stock | discontinued | coming_soon
    description      TEXT NOT NULL DEFAULT '',
    specs_json       TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL DEFAULT '',
    search_blob      TEXT NOT NULL DEFAULT ''  -- nome + marca + categoria + descrição + specs, normalizado
);

CREATE INDEX idx_products_category     ON products(category_id);
CREATE INDEX idx_products_price        ON products(price_brl);
CREATE INDEX idx_products_availability ON products(availability);

CREATE TABLE customers (
    customer_id   INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    name_norm     TEXT NOT NULL,
    phone         TEXT NOT NULL DEFAULT '',
    phone_digits  TEXT NOT NULL DEFAULT '',   -- só dígitos, para casar com qualquer formatação
    email         TEXT NOT NULL DEFAULT '',
    email_norm    TEXT NOT NULL DEFAULT '',
    city          TEXT NOT NULL DEFAULT '',
    city_norm     TEXT NOT NULL DEFAULT '',
    is_metro_cg   INTEGER NOT NULL DEFAULT 0  -- região metropolitana de Campo Grande (regra de frete)
);

CREATE INDEX idx_customers_phone ON customers(phone_digits);
CREATE INDEX idx_customers_name  ON customers(name_norm);

CREATE TABLE orders (
    order_id            INTEGER PRIMARY KEY,
    customer_id         INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date          TEXT NOT NULL,
    status              TEXT NOT NULL,   -- pending | confirmed | shipped | delivered | cancelled
    total_brl           REAL NOT NULL,
    payment_method      TEXT NOT NULL DEFAULT '',
    tracking_code       TEXT NOT NULL DEFAULT '',
    estimated_delivery  TEXT NOT NULL DEFAULT '',
    notes               TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_orders_customer ON orders(customer_id);

CREATE TABLE order_items (
    order_id    INTEGER NOT NULL REFERENCES orders(order_id),
    product_id  INTEGER NOT NULL REFERENCES products(product_id),
    quantity    INTEGER NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (order_id, product_id)
);

CREATE TABLE promotions (
    promotion_id      INTEGER PRIMARY KEY,
    product_id        INTEGER NOT NULL REFERENCES products(product_id),
    discount_percent  REAL NOT NULL CHECK (discount_percent >= 0 AND discount_percent <= 100),
    description       TEXT NOT NULL DEFAULT '',
    is_active         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_promotions_product ON promotions(product_id, is_active);

-- Preço vigente de cada produto.
--
-- O manual (§6.2) determina que promoções NÃO são cumulativas. A base tem
-- produtos com mais de uma promoção cadastrada, então a view escolhe uma só:
-- o maior desconto ENTRE AS ATIVAS. Promoções com is_active = 0 são ignoradas
-- por completo — §7.3 proíbe prometer desconto fora de vigência.
CREATE VIEW v_product_pricing AS
SELECT
    p.product_id,
    p.price_brl AS list_price,
    COALESCE(
        (SELECT MAX(pr.discount_percent)
           FROM promotions pr
          WHERE pr.product_id = p.product_id AND pr.is_active = 1),
        0
    ) AS active_discount_percent,
    (SELECT pr.description
       FROM promotions pr
      WHERE pr.product_id = p.product_id AND pr.is_active = 1
      ORDER BY pr.discount_percent DESC, pr.promotion_id ASC
      LIMIT 1
    ) AS active_promotion_label
FROM products p;

-- Metadados do ETL (versão do build, data, contagens) para rastreabilidade.
CREATE TABLE meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
