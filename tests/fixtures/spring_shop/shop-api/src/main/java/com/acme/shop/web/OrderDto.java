package com.acme.shop.web;

import com.acme.shop.domain.Order;

public record OrderDto(Long id, String customerId, String status) {

    public static OrderDto of(Order order) {
        return new OrderDto(order.getId(), order.getCustomerId(), order.getStatus().name());
    }
}
