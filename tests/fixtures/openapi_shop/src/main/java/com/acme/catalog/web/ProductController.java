package com.acme.catalog.web;

import com.acme.catalog.api.CatalogV2Api;
import com.acme.catalog.dto.ProductDto;
import com.acme.catalog.service.ProductService;
import java.util.List;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** Implements the API interface generated from api/openapi.yaml (not in the repository). */
@RestController
@RequestMapping("/v2")
public class ProductController implements CatalogV2Api {

    private final ProductService productService;

    public ProductController(ProductService productService) {
        this.productService = productService;
    }

    @Override
    public ResponseEntity<List<ProductDto>> listProducts(String category) {
        return ResponseEntity.ok(productService.findAll(category));
    }

    @Override
    @PreAuthorize("hasRole('CATALOG_ADMIN')")
    public ResponseEntity<ProductDto> createProduct(ProductDto product) {
        return ResponseEntity.status(201).body(productService.save(product));
    }

    @Override
    public ResponseEntity<?> getProduct(Integer productId) {
        return ResponseEntity.ok(productService.find(productId));
    }
}
