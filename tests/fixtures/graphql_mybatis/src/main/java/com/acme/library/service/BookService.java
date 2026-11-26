package com.acme.library.service;

import com.acme.library.domain.Book;
import com.acme.library.repository.BookRepository;
import java.util.List;
import org.springframework.stereotype.Service;

@Service
public class BookService {

    private final BookRepository bookRepository;

    public BookService(BookRepository bookRepository) {
        this.bookRepository = bookRepository;
    }

    public Book find(String id) {
        return bookRepository.find(id);
    }

    public Book add(String title, String authorId) {
        Book book = new Book(null, title, authorId);
        bookRepository.save(book);
        return book;
    }

    public List<Book> all() {
        return List.of();
    }
}
