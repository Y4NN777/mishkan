pub fn fixture() -> &'static str {
    "rust-ok"
}

#[cfg(test)]
mod tests {
    #[test]
    fn fixture_is_available() {
        assert_eq!(super::fixture(), "rust-ok");
    }
}
