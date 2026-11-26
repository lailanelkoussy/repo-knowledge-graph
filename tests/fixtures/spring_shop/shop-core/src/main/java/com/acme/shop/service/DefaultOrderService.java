package com.acme.shop.service;

import com.acme.shop.domain.Order;
import com.acme.shop.domain.OrderLine;
import com.acme.shop.messaging.Topics;
import com.acme.shop.repository.OrderRepository;
import java.util.Optional;
import lombok.RequiredArgsConstructor;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.context.annotation.Primary;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** Default order workflow: persists the order, then notifies the rest of the system. */
@Service
@Primary
@Transactional
@RequiredArgsConstructor
public class DefaultOrderService implements OrderService {

    private final OrderRepository orderRepository;
    private final ApplicationEventPublisher events;
    private final KafkaTemplate<String, String> kafkaTemplate;

    @Override
    public Order create(CreateOrderCommand command) {
        Order order = new Order();
        order.setCustomerId(command.customerId());
        OrderLine line = new OrderLine();
        line.setSku(command.sku());
        line.setQuantity(command.quantity());
        order.addLine(line);
        Order saved = orderRepository.save(order);
        events.publishEvent(new OrderCreatedEvent(saved.getId(), saved.getCustomerId()));
        kafkaTemplate.send(Topics.ORDER_CREATED, String.valueOf(saved.getId()));
        return saved;
    }

    @Override
    @Transactional(readOnly = true)
    public Optional<Order> find(Long id) {
        return orderRepository.findById(id);
    }
}
