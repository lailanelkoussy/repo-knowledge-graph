package com.acme.shop.client;

import com.acme.shop.config.PaymentProperties;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestTemplate;
import com.acme.shop.service.OrderService;

@Component
public class InventoryGateway {

    @Autowired
    private RestTemplate restTemplate;

    @Autowired
    @Qualifier("legacyOrders")
    private OrderService legacyOrders;

    @Value("${app.inventory.url:http://inventory}")
    private String inventoryUrl;

    private final PaymentProperties paymentProperties;

    public InventoryGateway(PaymentProperties paymentProperties) {
        this.paymentProperties = paymentProperties;
    }

    public String reserve(String sku) {
        return restTemplate.postForObject("http://inventory/api/stock/reserve", sku, String.class);
    }

    public String echoOrder(Long id) {
        return restTemplate.getForObject("/api/orders/{id}", String.class, id);
    }
}
