package com.acme.vuln.service;

import com.acme.vuln.repository.UserRepository;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public class UserService {

    private final UserRepository userRepository;

    public UserService(UserRepository userRepository) {
        this.userRepository = userRepository;
    }

    public List<Map<String, Object>> findByName(String name) {
        return userRepository.findByNameUnsafe(name);
    }
}
