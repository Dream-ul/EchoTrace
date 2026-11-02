import axios from 'axios'
import { ElMessage } from 'element-plus'

declare module 'axios' {
  interface AxiosRequestConfig {
    suppressErrorMessage?: boolean
  }
}

const client = axios.create({
  baseURL: '',
  timeout: 120000,
  headers: { 'Content-Type': 'application/json' },
})

client.interceptors.response.use(
  (response) => response,
  (error) => {
    const message = error.response?.data?.error || error.response?.data?.message || error.message || '网络请求失败'
    if (!error.config?.suppressErrorMessage) ElMessage.error(message)
    return Promise.reject(error)
  },
)

export default client
