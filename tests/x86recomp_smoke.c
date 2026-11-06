/* A small 32-bit x86 program for the x86recomp smoke test (tests/x86recomp_smoke.py):
 * a loop, 64-bit division (libgcc's __divdi3: bsr), float conversions and bswap. */
int g[64];
int f(int n){int s=0;for(int i=0;i<n;i++){s+=g[i&63]*i; if(s>1000) s^=n;} return s;}
long long d(long long a,long long b){return a/b+a%b;}
float h(float x){return x*1.5f+(float)(int)x;}
unsigned sw(unsigned x){return __builtin_bswap32(x);}
int main(int c,char**v){return f(c)+(int)d(c,3)+(int)h(c)+(int)sw((unsigned)c);}
