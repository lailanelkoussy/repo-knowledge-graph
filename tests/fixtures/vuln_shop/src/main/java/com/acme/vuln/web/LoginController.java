package com.acme.vuln.web;

import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestParam;

@Controller
public class LoginController {

    @GetMapping("/login/done")
    public String done(@RequestParam String next) {
        return "redirect:" + next;
    }

    @GetMapping("/owners/{ownerId}/back")
    public String back(@PathVariable Long ownerId) {
        return "redirect:/owners/" + ownerId;
    }
}
