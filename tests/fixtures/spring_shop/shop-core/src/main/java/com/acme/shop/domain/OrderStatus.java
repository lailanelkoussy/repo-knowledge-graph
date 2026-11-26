package com.acme.shop.domain;

public enum OrderStatus {
    NEW,
    PAID,
    CANCELLED;

    public boolean isFinal() {
        return this != NEW;
    }
}
