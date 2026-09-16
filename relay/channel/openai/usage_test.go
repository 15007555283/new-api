package openai

import (
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/QuantumNous/new-api/common"
	"github.com/QuantumNous/new-api/constant"
	relaycommon "github.com/QuantumNous/new-api/relay/common"
	"github.com/QuantumNous/new-api/relaykit/dto"
	"github.com/QuantumNous/new-api/relaykit/types"
	"github.com/gin-gonic/gin"
	"github.com/samber/lo"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestApplyUsagePostProcessingExposesPromptCacheHitTokens(t *testing.T) {
	usage := &dto.Usage{
		PromptTokensDetails: dto.InputTokenDetails{
			CachedTokens: 42,
		},
	}

	modified := applyUsagePostProcessing(&relaycommon.RelayInfo{
		ChannelMeta: &relaycommon.ChannelMeta{ChannelType: constant.ChannelTypeOpenAI},
	}, usage, nil)

	require.True(t, modified)
	require.Equal(t, 42, usage.PromptTokensDetails.CachedTokens)
	require.Equal(t, 42, usage.GetPromptCacheHitTokens())
}

func TestOpenaiHandlerExposesPromptCacheHitTokensInResponse(t *testing.T) {
	oldMode := gin.Mode()
	gin.SetMode(gin.TestMode)
	t.Cleanup(func() { gin.SetMode(oldMode) })

	body := `{"id":"chatcmpl_1","object":"chat.completion","created":1710000000,"model":"gpt-test","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"completion_tokens":5,"total_tokens":105,"prompt_tokens_details":{"cached_tokens":42}}}`
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	info := &relaycommon.RelayInfo{
		ChannelMeta: &relaycommon.ChannelMeta{
			ChannelType: constant.ChannelTypeOpenAI,
		},
		RelayFormat: types.RelayFormatOpenAI,
	}
	resp := &http.Response{
		StatusCode: http.StatusOK,
		Body:       io.NopCloser(strings.NewReader(body)),
		Header:     http.Header{"Content-Type": []string{"application/json"}},
	}

	usage, apiErr := OpenaiHandler(c, info, resp)

	require.Nil(t, apiErr)
	require.NotNil(t, usage)
	require.Equal(t, 42, usage.PromptTokensDetails.CachedTokens)
	require.Equal(t, 42, usage.GetPromptCacheHitTokens())
	assert.Contains(t, recorder.Body.String(), `"prompt_cache_hit_tokens":42`)
	assert.NotContains(t, recorder.Body.String(), `"prompt_cache_miss_tokens"`)
}

func TestDeepSeekCacheUsageResponse(t *testing.T) {
	oldTimeout := constant.StreamingTimeout
	constant.StreamingTimeout = 30
	t.Cleanup(func() { constant.StreamingTimeout = oldTimeout })
	oldMode := gin.Mode()
	gin.SetMode(gin.TestMode)
	t.Cleanup(func() { gin.SetMode(oldMode) })
	cases := []struct {
		name   string
		fields string
		hit    *int
		miss   *int
	}{
		{"uncached", `"prompt_tokens_details":{"cached_tokens":0}`, lo.ToPtr(0), lo.ToPtr(100)},
		{"partial", `"prompt_tokens_details":{"cached_tokens":42}`, lo.ToPtr(42), lo.ToPtr(58)},
		{"full", `"prompt_tokens_details":{"cached_tokens":100}`, lo.ToPtr(100), lo.ToPtr(0)},
		{"native_zero", `"prompt_cache_hit_tokens":0,"prompt_cache_miss_tokens":100`, lo.ToPtr(0), lo.ToPtr(100)},
		{"native_full", `"prompt_cache_hit_tokens":100,"prompt_cache_miss_tokens":0`, lo.ToPtr(100), lo.ToPtr(0)},
		{"preserve_native", `"prompt_cache_hit_tokens":40,"prompt_cache_miss_tokens":59`, lo.ToPtr(40), lo.ToPtr(59)},
		{"unknown", `"completion_tokens_details":{"reasoning_tokens":2}`, nil, nil},
		{"invalid_hit", `"prompt_cache_hit_tokens":101`, lo.ToPtr(101), nil},
	}
	for _, tc := range cases {
		for _, stream := range []bool{false, true} {
			for _, force := range []bool{false, true} {
				t.Run(fmt.Sprintf("%s/stream=%t/force=%t", tc.name, stream, force), func(t *testing.T) {
					rawUsage := `{"prompt_tokens":100,"completion_tokens":5,"total_tokens":105,` + tc.fields + `}`
					body := `{"id":"test","model":"deepseek-v4-flash","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],"usage":` + rawUsage + `}`
					if stream {
						body = "data: {\"id\":\"test\",\"choices\":[{\"index\":0,\"delta\":{\"reasoning_content\":\"think\"}}]}\n\n" +
							"data: {\"id\":\"test\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\n" +
							"data: {\"id\":\"test\",\"model\":\"deepseek-v4-flash\",\"choices\":[],\"usage\":" + rawUsage + "}\n\ndata: [DONE]\n\n"
					}
					recorder := httptest.NewRecorder()
					c, _ := gin.CreateTestContext(recorder)
					c.Request = httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
					info := &relaycommon.RelayInfo{
						ChannelMeta: &relaycommon.ChannelMeta{ChannelType: constant.ChannelTypeOpenAI, UpstreamModelName: "deepseek-v4-flash"},
						RelayFormat: types.RelayFormatOpenAI, ShouldIncludeUsage: true, DisablePing: true, StartTime: time.Now(),
					}
					info.ChannelSetting.ForceFormat = force
					resp := &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header)}
					var usage *dto.Usage
					var apiErr *types.NewAPIError
					if stream {
						usage, apiErr = OaiStreamHandler(c, info, resp)
					} else {
						usage, apiErr = OpenaiHandler(c, info, resp)
					}
					require.Nil(t, apiErr)
					require.NotNil(t, usage)
					output := recorder.Body.String()
					if stream {
						assert.Contains(t, output, `"reasoning_content":"think"`)
						assert.Contains(t, output, "data: [DONE]")
						var usageEvents []string
						for _, line := range strings.Split(output, "\n") {
							if strings.HasPrefix(line, "data: ") && strings.Contains(line, `"usage":{`) {
								usageEvents = append(usageEvents, strings.TrimPrefix(line, "data: "))
							}
						}
						require.Len(t, usageEvents, 1)
						output = usageEvents[0]
					}
					var payload struct {
						Usage map[string]any `json:"usage"`
					}
					require.NoError(t, common.Unmarshal([]byte(output), &payload))
					for key, want := range map[string]*int{"prompt_cache_hit_tokens": tc.hit, "prompt_cache_miss_tokens": tc.miss} {
						if want == nil {
							assert.NotContains(t, payload.Usage, key)
						} else {
							assert.Equal(t, float64(*want), payload.Usage[key], key)
						}
					}
					assert.Equal(t, float64(100), payload.Usage["prompt_tokens"])
				})
			}
		}
	}
}

func TestStreamPenultimateUsagePreservesNativeCacheCounts(t *testing.T) {
	oldTimeout := constant.StreamingTimeout
	constant.StreamingTimeout = 30
	t.Cleanup(func() { constant.StreamingTimeout = oldTimeout })
	body := "data: {\"id\":\"test\",\"choices\":[],\"usage\":{\"prompt_tokens\":100,\"completion_tokens\":5,\"total_tokens\":105,\"prompt_cache_hit_tokens\":40,\"prompt_cache_miss_tokens\":59}}\n\ndata: {\"id\":\"test\",\"choices\":[]}\n\ndata: [DONE]\n\n"
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	info := &relaycommon.RelayInfo{
		ChannelMeta: &relaycommon.ChannelMeta{ChannelType: constant.ChannelTypeOpenAI, UpstreamModelName: "deepseek-v4-flash"},
		RelayFormat: types.RelayFormatOpenAI, ShouldIncludeUsage: true, DisablePing: true, StartTime: time.Now(),
	}
	resp := &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header)}
	usage, apiErr := OaiStreamHandler(c, info, resp)
	require.Nil(t, apiErr)
	require.NotNil(t, usage)
	require.NotNil(t, usage.PromptCacheMissTokens)
	assert.Equal(t, 100, usage.PromptTokens)
	assert.Equal(t, 40, usage.GetPromptCacheHitTokens())
	assert.Equal(t, 59, *usage.PromptCacheMissTokens)
	assert.Equal(t, 1, strings.Count(recorder.Body.String(), `"usage":{`))
}
