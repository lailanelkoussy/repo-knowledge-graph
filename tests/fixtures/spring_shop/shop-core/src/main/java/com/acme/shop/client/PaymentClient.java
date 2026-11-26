package com.acme.shop.client;

import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;

@FeignClient(name = "payment-service", path = "/api/payments")
public interface PaymentClient {

    @PostMapping("/{orderId}")
    PaymentResult pay(@PathVariable("orderId") Long orderId);

    record PaymentResult(String status) {
    }
}
