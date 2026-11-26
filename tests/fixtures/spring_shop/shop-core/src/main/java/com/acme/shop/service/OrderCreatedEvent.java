package com.acme.shop.service;

public record OrderCreatedEvent(Long orderId, String customerId) {
}
