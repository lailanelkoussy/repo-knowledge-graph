package com.acme.vuln.messaging;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/** Asynchronous flow: a request parameter is published on a topic, the listener concatenates it into SQL. */
@RestController
class OrderController {

    private final KafkaTemplate<String, String> kafkaTemplate;

    OrderController(KafkaTemplate<String, String> kafkaTemplate) {
        this.kafkaTemplate = kafkaTemplate;
    }

    @PostMapping("/orders/notes")
    public String addNote(@RequestParam String note) {
        kafkaTemplate.send("order-notes", note);
        return "queued";
    }
}

@Component
class OrderEvents {

    private final JdbcTemplate jdbc;

    OrderEvents(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @KafkaListener(topics = "order-notes")
    public void onNote(String note) {
        jdbc.update("INSERT INTO notes(text) VALUES ('" + note + "')");
    }
}
