package com.acme.shop.repository;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.orm.jpa.DataJpaTest;

@DataJpaTest
class OrderRepositoryTest {

    @Autowired
    private OrderRepository repository;

    @Test
    void findsOrdersOfACustomer() {
        repository.findForCustomer("c-1");
    }
}
