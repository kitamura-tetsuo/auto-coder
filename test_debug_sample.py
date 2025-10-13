#!/usr/bin/env python3
"""
Sample Python script for testing MCP-PDB debugging functionality.
"""


def calculate_factorial(n):
    """Calculate factorial of n."""
    if n < 0:
        raise ValueError("Factorial is not defined for negative numbers")
    elif n == 0 or n == 1:
        return 1
    else:
        result = 1
        for i in range(2, n + 1):
            result *= i
        return result


def fibonacci(n):
    """Calculate the nth Fibonacci number."""
    if n < 0:
        raise ValueError("Fibonacci is not defined for negative numbers")
    elif n == 0:
        return 0
    elif n == 1:
        return 1
    else:
        a, b = 0, 1
        for _ in range(2, n + 1):
            a, b = b, a + b
        return b


def main():
    """Main function to test the debugging functionality."""
    print("Testing MCP-PDB debugging functionality")

    # Test factorial
    numbers = [0, 1, 5, 10]
    for num in numbers:
        try:
            fact = calculate_factorial(num)
            print(f"Factorial of {num} is {fact}")
        except ValueError as e:
            print(f"Error calculating factorial of {num}: {e}")

    # Test fibonacci
    fib_numbers = [0, 1, 5, 10, 15]
    for num in fib_numbers:
        try:
            fib = fibonacci(num)
            print(f"Fibonacci number at position {num} is {fib}")
        except ValueError as e:
            print(f"Error calculating Fibonacci of {num}: {e}")

    # Intentional bug for debugging
    try:
        buggy_result = calculate_factorial(-5)  # This will raise an error
        print(f"This should not print: {buggy_result}")
    except ValueError as e:
        print(f"Caught expected error: {e}")


if __name__ == "__main__":
    main()
