package com.acme.shop.web;

import com.acme.shop.domain.Order;
import com.acme.shop.service.CreateOrderCommand;
import com.acme.shop.service.OrderNotFoundException;
import com.acme.shop.service.OrderService;
import jakarta.validation.Valid;
import java.util.List;
import org.springframework.http.HttpStatus;
import org.springframework.security.access.annotation.Secured;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.*;

/** Orders REST API. */
@RestController
@RequestMapping(ApiPaths.ORDERS)
@Validated
public class OrderController {

    private final OrderService orderService;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    /** Returns one order. */
    @GetMapping("/{id}")
    public OrderDto get(@PathVariable Long id) {
        Order order = orderService.find(id).orElseThrow(() -> new OrderNotFoundException(id));
        return OrderDto.of(order);
    }

    @GetMapping
    public List<OrderDto> list(@RequestParam(defaultValue = "0") int page) {
        return List.of();
    }

    /** Creates an order and publishes an order-created event. */
    @PostMapping(consumes = "application/json")
    @PreAuthorize("hasRole('USER')")
    @ResponseStatus(HttpStatus.CREATED)
    public OrderDto create(@Valid @RequestBody CreateOrderRequest request) {
        Order order = orderService.create(new CreateOrderCommand(request.customerId(), request.sku(), request.quantity()));
        return OrderDto.of(order);
    }

    @DeleteMapping("/{id}")
    @Secured("ROLE_ADMIN")
    public void delete(@PathVariable Long id) {
    }
}
