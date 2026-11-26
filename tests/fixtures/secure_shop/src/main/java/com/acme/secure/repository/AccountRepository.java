package com.acme.secure.repository;

import com.acme.secure.model.Account;
import org.springframework.data.jpa.repository.JpaRepository;

public interface AccountRepository extends JpaRepository<Account, Long> {

    Account findByIdAndOwner(Long id, String owner);
}
