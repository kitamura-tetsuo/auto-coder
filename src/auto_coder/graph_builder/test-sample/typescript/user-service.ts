/**
 * User service module
 */

/**
 * User interface representing a user entity
 */
export interface User {
  id: string;
  name: string;
  email: string;
  age?: number;
}

/**
 * User repository type
 */
export type UserRepository = Map<string, User>;

/**
 * User service class for managing users
 */
export class UserService {
  private users: UserRepository = new Map();

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
    if (this.validateEmail(user.email)) {
      this.users.set(user.id, user);
    }
  }

  /**
   * Update existing user
   * @param id User ID
   * @param updates Partial user updates
   */
  updateUser(id: string, updates: Partial<User>): boolean {
    const user = this.getUserById(id);
    if (!user) {
      return false;
    }
    
    const updated = { ...user, ...updates };
    this.users.set(id, updated);
    return true;
  }

  /**
   * Delete user by ID
   * @param id User ID
   */
  deleteUser(id: string): boolean {
    return this.users.delete(id);
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

  /**
   * Get all users
   * @returns Array of all users
   */
  getAllUsers(): User[] {
    return Array.from(this.users.values());
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
 * Calculate user age from birth year
 * @param birthYear Birth year
 * @returns Current age
 */
export function calculateAge(birthYear: number): number {
  const currentYear = new Date().getFullYear();
  return currentYear - birthYear;
}

/**
 * Format user display name
 * @param user User object
 * @returns Formatted name
 */
export function formatUserName(user: User): string {
  return `${user.name} (${user.email})`;
}

/**
 * Check if user is adult
 * @param age User age
 * @returns True if adult
 */
export function isAdult(age: number): boolean {
  if (age >= 18) {
    return true;
  } else {
    return false;
  }
}

