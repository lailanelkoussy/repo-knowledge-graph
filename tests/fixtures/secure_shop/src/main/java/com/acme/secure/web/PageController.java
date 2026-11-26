package com.acme.secure.web;

import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestParam;

@Controller
public class PageController {

    @GetMapping("/")
    public String home() {
        return "home";
    }

    /** Public form: state-changing request reachable without authentication. */
    @PostMapping("/contact")
    public String contact(@RequestParam String message) {
        return "thanks";
    }

    @GetMapping("/dashboard")
    public String dashboard() {
        return "dashboard";
    }

    @ExceptionHandler(IllegalStateException.class)
    public ResponseEntity<String> failure(IllegalStateException ex) {
        return ResponseEntity.internalServerError().body(ex.getMessage());
    }
}
