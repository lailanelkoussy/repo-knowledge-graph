import { formatPrice } from './format';

export interface CartLine {
  name: string;
  price: number;
}

export class ApiClient {
  constructor(private baseUrl: string) {}

  async fetchCart(): Promise<CartLine[]> {
    const response = await fetch('/api/cart');
    return response.json();
  }
}

export function displayTotal(lines: CartLine[]): string {
  return formatPrice(lines.reduce((acc, l) => acc + l.price, 0));
}
