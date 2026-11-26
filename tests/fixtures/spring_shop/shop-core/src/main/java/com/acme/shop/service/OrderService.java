package com.acme.shop.service;

import com.acme.shop.domain.Order;
import java.util.Optional;

public interface OrderService {

    Order create(CreateOrderCommand command);

    Optional<Order> find(Long id);
}
