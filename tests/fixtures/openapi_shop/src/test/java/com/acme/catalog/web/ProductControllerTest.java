package com.acme.catalog.web;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(ProductController.class)
class ProductControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void getsOneProduct() throws Exception {
        mockMvc.perform(get("/v2/products/1"));
        mockMvc.perform(get("/v2/unknown"));
    }
}
