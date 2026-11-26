package com.acme.shop.web;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** Admin endpoints, written with Java 21 syntax. */
@RestController
@RequestMapping("/api/admin")
public class AdminController {

    sealed interface Metric permits Count, Ratio {
    }

    record Count(long value) implements Metric {
    }

    record Ratio(double value) implements Metric {
    }

    @GetMapping({"/stats", "/statistics"})
    public String stats() {
        Metric metric = new Count(3);
        String label = switch (metric) {
            case Count c -> "count " + c.value();
            case Ratio r -> "ratio " + r.value();
        };
        return """
            {"metric": "%s"}
            """.formatted(label);
    }
}
