-- Initial schema
CREATE TABLE orders (
    id BIGSERIAL PRIMARY KEY,
    customer_id VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    created_at TIMESTAMP NOT NULL,
    version BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE order_line (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders (id),
    sku VARCHAR(64) NOT NULL,
    quantity INT NOT NULL,
    unit_price NUMERIC(10, 2),
    CONSTRAINT positive_quantity CHECK (quantity > 0)
);
