package com.acme.shop.service;

public record CreateOrderCommand(String customerId, String sku, int quantity) {
}
