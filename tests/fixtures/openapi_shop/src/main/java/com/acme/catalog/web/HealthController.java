package com.acme.catalog.web;

import com.acme.catalog.api.HealthApi;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class HealthController implements HealthApi {

    @Override
    public ResponseEntity<Void> health() {
        return ResponseEntity.ok().build();
    }
}
