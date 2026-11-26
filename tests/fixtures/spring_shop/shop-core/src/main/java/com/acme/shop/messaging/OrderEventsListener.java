package com.acme.shop.messaging;

import com.acme.shop.service.OrderCreatedEvent;
import org.springframework.context.event.EventListener;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.scheduling.annotation.Async;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Component
public class OrderEventsListener {

    @KafkaListener(topics = "${app.kafka.payments-topic}", groupId = "shop")
    public void onPayment(String payload) {
        markPaid(payload);
    }

    @EventListener
    @Async
    public void onOrderCreated(OrderCreatedEvent event) {
        markPaid(String.valueOf(event.orderId()));
    }

    @Scheduled(cron = "${app.cleanup.cron}")
    public void cleanup() {
    }

    private void markPaid(String orderId) {
    }
}
