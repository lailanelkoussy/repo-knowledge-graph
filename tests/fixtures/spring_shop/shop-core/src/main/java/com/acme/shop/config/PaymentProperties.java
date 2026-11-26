package com.acme.shop.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "app.payment")
public record PaymentProperties(int timeoutMs, String baseUrl, int retries) {
}
