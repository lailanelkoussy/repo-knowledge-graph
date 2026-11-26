package com.acme.shop.repository;

import com.acme.shop.domain.Order;
import com.acme.shop.domain.OrderStatus;
import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;

public interface OrderRepository extends JpaRepository<Order, Long> {

    List<Order> findByStatusAndCustomerIdOrderByCreatedAtDesc(OrderStatus status, String customerId);

    @Query("select o from Order o where o.customerId = :customerId")
    List<Order> findForCustomer(String customerId);

    @Query(value = "SELECT * FROM orders o JOIN order_line l ON l.order_id = o.id WHERE o.status = 'NEW'", nativeQuery = true)
    List<Order> findNewWithLines();
}
