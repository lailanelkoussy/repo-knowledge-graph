package com.acme.catalog.service;

import com.acme.catalog.dto.ProductDto;
import java.util.List;
import org.springframework.stereotype.Service;

@Service
public class ProductService {

    public List<ProductDto> findAll(String category) {
        return List.of();
    }

    public ProductDto find(Integer id) {
        return null;
    }

    public ProductDto save(ProductDto product) {
        return product;
    }
}
