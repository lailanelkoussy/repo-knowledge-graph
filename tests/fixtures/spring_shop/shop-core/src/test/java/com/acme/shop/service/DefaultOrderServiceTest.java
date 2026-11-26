package com.acme.shop.service;

import com.acme.shop.repository.OrderRepository;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

@ExtendWith(MockitoExtension.class)
class DefaultOrderServiceTest {

    @Mock
    private OrderRepository orderRepository;

    @InjectMocks
    private DefaultOrderService service;

    @Test
    void findDelegatesToTheRepository() {
        service.find(1L);
    }
}
