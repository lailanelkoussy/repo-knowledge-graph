"""
Test script for RustEntityExtractor
"""

from repo_knowledge_graph.entity_extractor import RustEntityExtractor

# Sample Rust code with various constructs
rust_code = """
use std::collections::HashMap;
use std::fmt::{Display, Debug};

/// A simple struct representing a user
pub struct User {
    pub id: u64,
    pub name: String,
    email: String,
}

/// An enum representing different user roles
pub enum Role {
    Admin,
    User,
    Guest,
}

/// A trait for authentication
pub trait Authenticator {
    fn authenticate(&self, password: &str) -> bool;
    fn is_admin(&self) -> bool;
}

impl User {
    /// Create a new user
    pub fn new(id: u64, name: String, email: String) -> Self {
        User { id, name, email }
    }

    /// Get the user's display name
    pub fn display_name(&self) -> &str {
        &self.name
    }

    /// Update the user's email
    pub fn update_email(&mut self, new_email: String) {
        self.email = new_email;
        println!("Email updated to: {}", self.email);
    }
}

impl Authenticator for User {
    fn authenticate(&self, password: &str) -> bool {
        // Simple authentication logic
        verify_password(password)
    }

    fn is_admin(&self) -> bool {
        false
    }
}

/// Verify a password
pub fn verify_password(password: &str) -> bool {
    password.len() >= 8
}

/// Create a sample user
pub fn create_sample_user() -> User {
    let user = User::new(1, String::from("Alice"), String::from("alice@example.com"));
    user
}

const MAX_USERS: usize = 1000;
static mut USER_COUNT: usize = 0;

mod api {
    use super::User;

    pub struct UserRepository {
        users: Vec<User>,
    }

    impl UserRepository {
        pub fn new() -> Self {
            UserRepository { users: Vec::new() }
        }

        pub fn add_user(&mut self, user: User) {
            self.users.push(user);
        }

        pub fn find_by_id(&self, id: u64) -> Option<&User> {
            self.users.iter().find(|u| u.id == id)
        }
    }
}

type UserId = u64;
type UserMap = HashMap<UserId, User>;

fn main() {
    let mut repo = api::UserRepository::new();
    let user = create_sample_user();
    repo.add_user(user);

    println!("User count: {}", unsafe { USER_COUNT });
    println!("Max users: {}", MAX_USERS);
}
"""

def test_rust_entity_extractor():
    """Test RustEntityExtractor with comprehensive Rust code."""

    extractor = RustEntityExtractor()
    declared, called = extractor.extract_entities(rust_code)


    # Group by type
    by_type = {}
    for entity in declared:
        entity_type = entity.get('type', 'unknown')
        if entity_type not in by_type:
            by_type[entity_type] = []
        by_type[entity_type].append(entity)

    for entity_type, entities in sorted(by_type.items()):
        for entity in entities:
            name = entity.get('name', 'N/A')
            dtype = entity.get('dtype', '')
            if dtype:
                pass
            else:
                pass


        for i, call in enumerate(called, 1):
            pass


        # Verify some expected entities

        declared_names = [e['name'] for e in declared]

        expected = [
            ('User', 'struct'),
            ('Role', 'enum'),
            ('Authenticator', 'trait'),
            ('User::new', 'method'),
            ('verify_password', 'function'),
            ('api', 'module'),
            ('api::UserRepository', 'struct'),
            ('MAX_USERS', 'constant'),
            ('USER_COUNT', 'static'),
            ('UserId', 'type_alias'),
        ]

        for name, expected_type in expected:
            found = any(e['name'] == name and e['type'] == expected_type for e in declared)
            status = "✓" if found else "✗"


    # Assertions for pytest
    assert declared is not None, "Declared entities should not be None"
    assert called is not None, "Called entities should not be None"
    assert len(declared) > 0, "Should have at least one declared entity"

    # Verify key expected entities are found
    declared_names = [e['name'] for e in declared]
    assert 'User' in declared_names, "User struct should be found"
    assert 'Role' in declared_names, "Role enum should be found"
    assert 'Authenticator' in declared_names, "Authenticator trait should be found"
