package com.acme.shop.service;

import com.acme.shop.domain.Order;
import java.util.Optional;
import org.springframework.stereotype.Service;

@Service("legacyOrders")
public class LegacyOrderService implements OrderService {

    @Override
    public Order create(CreateOrderCommand command) {
        throw new UnsupportedOperationException("read only");
    }

    @Override
    public Optional<Order> find(Long id) {
        return Optional.empty();
    }
}
