/**
 * Sample TypeScript file for testing
 */

/**
 * User interface
 */
export interface User {
  id: string;
  name: string;
  email: string;
}

/**
 * User service class
 */
export class UserService {
  private users: Map<string, User> = new Map();

  /**
   * Get user by ID
   * @param id User ID
   * @returns User object or undefined
   */
  getUserById(id: string): User | undefined {
    return this.users.get(id);
  }

  /**
   * Create a new user
   * @param user User object
   */
  createUser(user: User): void {
    this.users.set(user.id, user);
  }

  /**
   * Validate user email
   * @param email Email address
   * @returns True if valid
   */
  validateEmail(email: string): boolean {
    const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    return emailRegex.test(email);
  }
}

/**
 * Fetch user from API
 * @param userId User ID
 * @returns Promise with user data
 */
export async function fetchUserFromAPI(userId: string): Promise<User> {
  const response = await fetch(`/api/users/${userId}`);
  const data = await response.json();
  return data as User;
}

/**
 * Calculate user age
 * @param birthYear Birth year
 * @returns Current age
 */
export function calculateAge(birthYear: number): number {
  const currentYear = new Date().getFullYear();
  return currentYear - birthYear;
}

